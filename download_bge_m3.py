from pathlib import Path
from urllib.parse import quote

import requests


REPO_ID = "BAAI/bge-m3"
BASE = f"https://hf-mirror.com/{REPO_ID}"
API_URL = f"https://hf-mirror.com/api/models/{REPO_ID}"
LOCAL_DIR = Path(r"D:\models\bge-m3")
LOCAL_DIR.mkdir(parents=True, exist_ok=True)


def get_files():
    r = requests.get(API_URL, timeout=30)
    r.raise_for_status()
    data = r.json()
    return [
        item["rfilename"]
        for item in data.get("siblings", [])
        if item.get("rfilename")
    ]


def download_file(filename):
    url = f"{BASE}/resolve/main/{quote(filename, safe='/')}"
    path = LOCAL_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)

    headers = {}
    mode = "wb"
    existing = path.stat().st_size if path.exists() else 0

    if existing > 0:
        headers["Range"] = f"bytes={existing}-"
        mode = "ab"
        print(f"续传: {filename} from {existing / 1024 / 1024:.1f}MB")
    else:
        print(f"下载: {filename}")

    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        if r.status_code == 416:
            print(f"  已完成，跳过: {filename}")
            return
        r.raise_for_status()

        downloaded = existing
        with open(path, mode) as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                print(f"\r  {downloaded / 1024 / 1024:.1f}MB", end="", flush=True)

    print()


def main():
    print(f"repo: {REPO_ID}")
    print(f"save: {LOCAL_DIR}")
    files = get_files()
    print(f"files: {len(files)}")
    for i, filename in enumerate(files, 1):
        print(f"[{i}/{len(files)}]")
        download_file(filename)
    print("完成")


if __name__ == "__main__":
    main()