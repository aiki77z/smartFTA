import subprocess
import sys
import argparse
import shutil

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
        # MinerU 可能在首次运行下载模型/耗时较长；但若长期无响应也需要尽早失败退出
        timeout_s = 15 * 60
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout_s)
        print("命令执行成功")
        if result.stdout:
            print("标准输出:", result.stdout)
    except FileNotFoundError:
        which = shutil.which("mineru")
        print("错误: 未找到 MinerU 命令行工具 `mineru`，无法执行 PDF -> Markdown 转换。")
        print(f"当前 PATH 中 mineru = {which}")
        print("请在当前虚拟环境中安装 MinerU，并确保其脚本目录在 PATH 中。")
        print('推荐（见 README）：uv pip install -U "mineru[all]" -i https://mirrors.aliyun.com/pypi/simple')
        print("或：python -m pip install -U \"mineru[all]\"")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"命令执行失败，返回码: {e.returncode}")
        if e.stderr:
            print("错误输出:", e.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"错误: MinerU 执行超时（>{timeout_s}s），可能卡在模型下载/环境依赖/解析阶段。")
        print("建议：先在命令行单独运行 mineru 对同一 PDF 做一次转换，观察是否有下载/报错输出。")
        sys.exit(1)

if __name__ == "__main__":
    args = parse_arguments()
    run_mineru(args)