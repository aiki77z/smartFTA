"""
import_chunks.py —— 一次性把JSON文件导入MongoDB

用法：
  python import_chunks.py --file output_with_keywords.json
"""

import json
import argparse
from database import import_chunks

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导入chunks到MongoDB")
    parser.add_argument("--file", required=True, help="JSON文件路径")
    args = parser.parse_args()

    with open(args.file, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    import_chunks(chunks)
    print("导入完成！")
