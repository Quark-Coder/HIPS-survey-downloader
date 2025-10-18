# HiPS Survey Downloader

A Python script for downloading **HiPS (Hierarchical Progressive Survey)** tiles from an online survey for offline use.
It automatically reads survey metadata, handles retries for network errors, and downloads tiles in parallel.

---

## Features

* Auto-detection of `hips_tile_format` and `hips_order` from survey properties
* Multi-threaded downloads with retries
* Optional interactive mode when parameters are missing
* Handles expected missing tiles (404) gracefully

---

## Installation

```bash
git clone https://github.com/Quark-Coder/HIPS-survey-downloader
cd hips-downloader
pip install requests
```
---

## Usage

Command-line mode:

```bash
python hips_downloader.py --url https://example.org/survey/ --output ./out_dir --max-order 3 --workers 4
```

Interactive mode (if any argument is omitted):

```bash
python hips_downloader.py
```

https://github.com/Stellarium/stellarium/discussions/4589



