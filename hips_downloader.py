import os
import sys
import re
import time
import argparse
import requests
from pathlib import Path
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

RETRYABLE_STATUS = {408, 429}  # plus all 5xx treated as retryable

def wait_for_exit():
    # Robust console hold: works for Windows double-click and non-interactive stdin
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        if os.name == 'nt':
            try:
                import msvcrt
                print("\nPress any key to exit...")
                msvcrt.getch()
            except Exception:
                os.system("pause")  # Fallback: "Press any key to continue . . ."
        else:
            input("\nPress Enter to exit...")
    except EOFError:
        # No interactive stdin; keep window visible briefly
        time.sleep(5)
    except Exception:
        time.sleep(5)

class HiPSDownloader:
    def __init__(self, base_url, output_dir, max_order, max_workers):
        self.base_url = base_url.rstrip('/') + '/'
        self.output_dir = Path(output_dir)
        self.user_max_order = max_order
        self.max_workers = max_workers

        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'HiPS-Downloader/1.0'})

        # Counters
        self.downloaded = 0
        self.skipped = 0
        self.missing_404 = 0        # expected missing tiles (404)
        self.failed_non404 = 0      # non-404 failures after retries

        # Deferred retry queue for transient errors only
        self.defer_queue = []

        # Derived from properties
        self.tile_ext = None
        self.survey_max_order = None
        self.effective_max_order = None

    # ------------------------
    # Properties handling
    # ------------------------
    def _fetch_properties_text(self, retries=3, base_delay=0.5):
        props_url = urljoin(self.base_url, 'properties')
        last_err = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.get(props_url, timeout=30)
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code == 404:
                    print("Properties returned 404; cannot proceed without survey metadata.")
                    self.failed_non404 += 1
                    return None
                if resp.status_code >= 500 or resp.status_code in RETRYABLE_STATUS:
                    last_err = f"HTTP {resp.status_code}"
                    time.sleep(base_delay * (attempt + 1))
                    continue
                print(f"Failed to fetch properties: HTTP {resp.status_code}")
                self.failed_non404 += 1
                return None
            except requests.RequestException as e:
                last_err = str(e)
                time.sleep(base_delay * (attempt + 1))
        print(f"Failed to fetch properties after retries: {last_err}")
        self.failed_non404 += 1
        return None

    @staticmethod
    def _parse_properties(text: str):
        """
        Parse key=value pairs from HiPS properties text.
        Accepts newline-separated lines and compact space-aligned formats.
        Comments starting with # are ignored, inline comments stripped.
        """
        props = {}
        cleaned_lines = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if '#' in line:
                line = line.split('#', 1)[0].strip()
            if line:
                cleaned_lines.append(line)
        blob = ' '.join(cleaned_lines)
        for k, v in re.findall(r'([A-Za-z0-9_]+)\s*=\s*([^\s#]+)', blob):
            props[k] = v
        return props

    @staticmethod
    def _map_tile_format_to_ext(fmt: str):
        fmt = (fmt or '').lower()
        if fmt in ('jpeg', 'jpg'):
            return 'jpg'
        if fmt in ('png', 'fits'):
            return fmt
        return None

    def load_and_apply_properties(self):
        print("Loading survey properties...")
        text = self._fetch_properties_text()
        if not text:
            raise RuntimeError("Cannot continue without properties.")

        # Save raw properties
        props_path = self.output_dir / 'properties'
        props_path.parent.mkdir(parents=True, exist_ok=True)
        props_path.write_text(text, encoding='utf-8')

        props = self._parse_properties(text)

        # STRICT naming: only these two keys
        if 'hips_tile_format' not in props:
            raise RuntimeError("Missing 'hips_tile_format' in properties.")
        if 'hips_order' not in props:
            raise RuntimeError("Missing 'hips_order' in properties.")

        self.tile_ext = self._map_tile_format_to_ext(props['hips_tile_format'])
        if not self.tile_ext:
            raise RuntimeError(f"Unrecognized hips_tile_format: {props['hips_tile_format']!r}")

        try:
            self.survey_max_order = int(props['hips_order'])
        except ValueError:
            raise RuntimeError(f"hips_order is not an integer: {props['hips_order']!r}")

        # Respect the survey’s published max order
        self.effective_max_order = min(self.user_max_order, self.survey_max_order)
        if self.effective_max_order < self.user_max_order:
            print(f"Limiting max order to {self.effective_max_order} per properties (hips_order).")

    # ------------------------
    # Download primitives
    # ------------------------
    def _attempt_once(self, url, local_path):
        """
        Returns one of: 'skipped', 'ok', 'missing', 'retryable', 'fatal'
        """
        try:
            if local_path.exists():
                self.skipped += 1
                return 'skipped'

            local_path.parent.mkdir(parents=True, exist_ok=True)
            resp = self.session.get(url, timeout=30, stream=True)
            code = resp.status_code

            if code == 200:
                with open(local_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                self.downloaded += 1
                return 'ok'

            if code == 404:
                return 'missing'

            if code >= 500 or code in RETRYABLE_STATUS:
                return 'retryable'

            return 'fatal'

        except requests.RequestException:
            return 'retryable'
        except Exception:
            return 'fatal'

    def download_classified(self, url, local_path, redownloads=3, base_delay=0.5):
        """
        Download with classification:
        - 404 => missing (no retries)
        - retryable => retries then defer
        - fatal => counted as non-404 failure
        """
        attempt = 0
        while True:
            result = self._attempt_once(url, local_path)
            if result in ('ok', 'skipped'):
                return True
            if result == 'missing':
                self.missing_404 += 1
                return False
            if result == 'fatal':
                self.failed_non404 += 1
                return False
            # retryable
            if attempt >= redownloads:
                self.defer_queue.append((url, local_path))
                self.failed_non404 += 1
                return False
            time.sleep(base_delay * (attempt + 1))
            attempt += 1

    def final_retry_pass(self, redownloads=3, base_delay=0.5):
        if not self.defer_queue:
            print("No transient failures to retry after initial pass.")
            return

        print("\nRetrying transient failures after initial pass...")
        pending = self.defer_queue
        self.defer_queue = []

        def worker(item):
            url, path = item
            attempt = 0
            while True:
                result = self._attempt_once(url, path)
                if result in ('ok', 'skipped'):
                    return 'recovered'
                if result == 'missing':
                    return 'now_missing'
                if result == 'fatal':
                    return 'still_failed'
                if attempt >= redownloads:
                    return 'still_failed'
                time.sleep(base_delay * (attempt + 1))
                attempt += 1

        recovered = 0
        converted_to_missing = 0
        still_failed = 0

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(worker, it): it for it in pending}
            for future in as_completed(futures):
                outcome = future.result()
                if outcome == 'recovered':
                    recovered += 1
                elif outcome == 'now_missing':
                    converted_to_missing += 1
                else:
                    still_failed += 1

        # Adjust counters: all deferred were counted as non-404 failures once
        self.failed_non404 -= recovered
        self.failed_non404 -= converted_to_missing
        self.missing_404 += converted_to_missing

        print(f"Post-pass retry complete: recovered {recovered}, now-missing {converted_to_missing}, remaining non-404 failures {still_failed}")

    # ------------------------
    # High-level downloads
    # ------------------------
    def download_allsky(self):
        # Try Allsky with discovered tile extension; ignore 404
        print("Downloading Allsky preview...")
        ext = self.tile_ext or 'jpg'
        allsky_url = urljoin(self.base_url, f'Norder3/Allsky.{ext}')
        allsky_path = self.output_dir / 'Norder3' / f'Allsky.{ext}'
        result = self._attempt_once(allsky_url, allsky_path)
        if result in ('ok', 'skipped'):
            print("✓ Allsky preview saved")
        elif result == 'missing':
            print("Allsky preview not present for this survey; skipping without error.")
        elif result == 'retryable':
            time.sleep(0.5)
            final = self._attempt_once(allsky_url, allsky_path)
            if final in ('ok', 'skipped'):
                print("✓ Allsky preview saved")
            elif final == 'missing':
                print("Allsky preview not present for this survey; skipping without error.")
            else:
                self.failed_non404 += 1
                print("Allsky preview failed after retry; continuing.")

    def get_tile_urls_for_order(self, order):
        npix_max = 12 * (4 ** order)
        ext = self.tile_ext
        tile_urls = []
        for npix in range(npix_max):
            dir_num = (npix // 10000) * 10000
            tile_path = f"Norder{order}/Dir{dir_num}/Npix{npix}.{ext}"
            url = urljoin(self.base_url, tile_path)
            local_path = self.output_dir / tile_path
            tile_urls.append((url, local_path))
        return tile_urls

    def download_order(self, order):
        print(f"\nDownloading Order {order}...")
        tile_urls = self.get_tile_urls_for_order(order)
        total_tiles = len(tile_urls)
        print(f"  Attempting {total_tiles} tiles...")

        failed_before = self.failed_non404
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.download_classified, url, path): (url, path) for url, path in tile_urls}
            completed = 0
            for future in as_completed(futures):
                completed += 1
                if completed % 100 == 0:
                    print(f"  Progress: {completed}/{total_tiles} tiles processed")

        queued_now = max(0, self.failed_non404 - failed_before)
        print(f"  Order {order} complete: {self.downloaded} new, {self.skipped} existing, {self.missing_404} missing (404), {queued_now} transient errors queued")

    def download_survey(self):
        print(f"Starting HiPS download from {self.base_url}")
        print(f"Output directory: {self.output_dir}")
        print(f"Requested max order: {self.user_max_order}")
        print(f"Parallel workers: {self.max_workers}")
        print("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Load properties and configure
        self.load_and_apply_properties()
        print(f"Tile extension: .{self.tile_ext}")
        print(f"Survey max order: {self.survey_max_order}")
        print(f"Effective max order: {self.effective_max_order}")

        # Optional item
        self.download_allsky()

        # Orders
        for order in range(self.effective_max_order + 1):
            start = time.time()
            self.download_order(order)
            print(f"  Order {order} took {time.time() - start:.1f} seconds")

        # Final retry for transient errors
        self.final_retry_pass()

        print("=" * 60)
        print("Totals:")
        print(f"  Downloaded: {self.downloaded}")
        print(f"  Skipped (existing): {self.skipped}")
        print(f"  Missing (404): {self.missing_404}")
        print(f"  Failed after retries (non-404): {self.failed_non404}")

        if self.failed_non404 == 0:
            print("All existing files downloaded successfully (ignoring expected 404s).")
        else:
            print("Some files failed after retries (non-404); see counts above.")

# ------------------------
# CLI & interactive input
# ------------------------
def is_valid_url(s: str) -> bool:
    try:
        u = urlparse(s.strip())
        return u.scheme in ('http', 'https') and u.netloc != ''
    except Exception:
        return False

def prompt_int(prompt: str, validator):
    while True:
        raw = input(prompt).strip()
        if raw == '':
            print("This value is required, please try again.")
            continue
        try:
            val = int(raw)
        except ValueError:
            print("Please enter an integer, try again.")
            continue
        if validator(val):
            return val
        print("Value out of allowed range, try again.")

def interactive_fill(args):
    while True:
        while not args.url or not is_valid_url(args.url):
            if args.url:
                print("URL is invalid (expecting http/https with a host).")
            print("Enter the base HiPS URL (e.g., https://example.org/survey/):")
            args.url = input("URL: ").strip()

        while not args.output:
            print("Enter the output directory for tiles (it will be created if needed):")
            args.output = input("Directory: ").strip()

        if args.max_order is None:
            args.max_order = prompt_int("Max order (0-8): ", lambda x: 0 <= x <= 8)
        else:
            if not (0 <= args.max_order <= 8):
                print("max-order must be in 0-8; please re-enter.")
                args.max_order = prompt_int("Max order (0-8): ", lambda x: 0 <= x <= 8)

        if args.workers is None:
            args.workers = prompt_int("Number of threads (>=1): ", lambda x: x >= 1)
        else:
            if args.workers < 1:
                print("workers must be >= 1; please re-enter.")
                args.workers = prompt_int("Number of threads (>=1): ", lambda x: x >= 1)

        print("\nConfirm parameters:")
        print(f"  URL:      {args.url}")
        print(f"  Directory:{args.output}")
        print(f"  MaxOrder: {args.max_order}")
        print(f"  Workers:  {args.workers}")
        confirm = input("Proceed? [Y/n]: ").strip().lower()
        if confirm in ('', 'y', 'yes'):
            return args
        args.url = None
        args.output = None
        args.max_order = None
        args.workers = None

def run():
    # Argparse without defaults; interactive on missing values
    parser = argparse.ArgumentParser(
        description='Download HiPS survey tiles for offline use',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples (no defaults; pass all params explicitly):
  python hips_downloader.py --url https://example.org/survey/ --output ./out_dir --max-order 3 --workers 4

If you omit parameters, the script will ask for them interactively.
        """,
        exit_on_error=False
    )

    parser.add_argument('--url', help='Base URL of HiPS survey')
    parser.add_argument('--output', help='Output directory for downloaded tiles')
    parser.add_argument('--max-order', help='Maximum HiPS order to download (0-8)')
    parser.add_argument('--workers', help='Number of parallel download threads')

    try:
        args = parser.parse_args()
    except Exception as e:
        print(f"Argument parser warning: {e}")
        args = argparse.Namespace(url=None, output=None, max_order=None, workers=None)

    if args.max_order is not None:
        try:
            args.max_order = int(args.max_order)
        except ValueError:
            args.max_order = None
    if args.workers is not None:
        try:
            args.workers = int(args.workers)
        except ValueError:
            args.workers = None

    missing = any(v in (None, '') for v in (args.url, args.output, args.max_order, args.workers))
    if missing:
        args = interactive_fill(args)

    downloader = HiPSDownloader(
        base_url=args.url,
        output_dir=args.output,
        max_order=args.max_order,
        max_workers=args.workers
    )

    downloader.download_survey()

if __name__ == '__main__':
    try:
        run()
    except KeyboardInterrupt:
        print("\n\nDownload interrupted by user")
    finally:
        wait_for_exit()
