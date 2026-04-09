import subprocess
import sys
import argparse

def parse_arguments():
    parser = argparse.ArgumentParser(description="运行 MinerU 命令")
    parser.add_argument("-i", "--input", required=True, help="输入的 PDF 文件路径")
    parser.add_argument("-o", "--output", default="./output", help="输出目录（默认: ./test）")
    parser.add_argument("-b", "--backend", default="pipeline", help="后端模式（默认: pipeline）")
    parser.add_argument("-m", "--mode", default="ocr", help="运行模式（默认: ocr）")
    return parser.parse_args()

def run_mineru(args):
    cmd = [
        "mineru",
        "-p", args.input,
        "-o", args.output,
        "-b", args.backend,
        "-m", args.mode
    ]
    
    try:
        print(f"执行命令: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        print("命令执行成功")
        if result.stdout:
            print("标准输出:", result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"命令执行失败，返回码: {e.returncode}")
        if e.stderr:
            print("错误输出:", e.stderr)
        sys.exit(1)

if __name__ == "__main__":
    args = parse_arguments()
    run_mineru(args)