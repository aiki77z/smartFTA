import subprocess
import sys
import argparse
import shutil
import os
import tempfile
import re
from pathlib import Path

def parse_arguments():
    parser = argparse.ArgumentParser(description="运行 MinerU 命令并自动管理文档版本")
    parser.add_argument("-i", "--input", required=True, help="输入的 PDF 文件路径")
    parser.add_argument("-o", "--output", default="./output", help="输出根目录（默认: ./output）")
    parser.add_argument("-b", "--backend", default="pipeline", help="后端模式（默认: pipeline）")
    parser.add_argument("-m", "--mode", default="ocr", help="运行模式（默认: ocr）")
    return parser.parse_args()

def get_next_version(output_root, file_id):
    """根据已有版本目录确定下一个版本号"""
    file_id_dir = Path(output_root) / file_id
    if not file_id_dir.exists():
        return 1

    pattern = re.compile(rf"^{re.escape(file_id)}_v(\d+)$")
    max_version = 0
    for item in file_id_dir.iterdir():
        if item.is_dir():
            match = pattern.match(item.name)
            if match:
                version = int(match.group(1))
                if version > max_version:
                    max_version = version
    return max_version + 1

def run_mineru(args, temp_output_dir):
    """调用 MinerU 命令，输出到临时目录"""
    cmd = [
        "mineru",
        "-p", args.input,
        "-o", str(temp_output_dir),
        "-b", args.backend,
        "-m", args.mode
    ]

    print(f"执行命令: {' '.join(cmd)}")
    timeout_s = 15 * 60  # 15 分钟超时
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout_s)
        print("MinerU 执行成功")
        if result.stdout:
            print("标准输出:", result.stdout)
        return True
    except FileNotFoundError:
        which = shutil.which("mineru")
        print("错误: 未找到 MinerU 命令行工具 `mineru`，无法执行 PDF -> Markdown 转换。")
        print(f"当前 PATH 中 mineru = {which}")
        print("请在当前虚拟环境中安装 MinerU，并确保其脚本目录在 PATH 中。")
        print('推荐（见 README）：uv pip install -U "mineru[all]" -i https://mirrors.aliyun.com/pypi/simple')
        print("或：python -m pip install -U \"mineru[all]\"")
        return False
    except subprocess.CalledProcessError as e:
        print(f"命令执行失败，返回码: {e.returncode}")
        if e.stderr:
            print("错误输出:", e.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"错误: MinerU 执行超时（>{timeout_s}s），可能卡在模型下载/环境依赖/解析阶段。")
        print("建议：先在命令行单独运行 mineru 对同一 PDF 做一次转换，观察是否有下载/报错输出。")
        return False

def move_mineru_output_to_version(temp_output_dir, file_id, version_dir):
    """
    将临时目录中 MinerU 生成的 <file_id> 子目录下的所有内容移动到版本目录
    """
    source_dir = Path(temp_output_dir) / file_id
    if not source_dir.exists() or not source_dir.is_dir():
        print(f"错误: 临时目录中未找到预期的输出子目录 {source_dir}")
        return False

    # 确保版本目录存在（空目录）
    version_dir.mkdir(parents=True, exist_ok=True)

    # 移动所有内容
    for item in source_dir.iterdir():
        dest = version_dir / item.name
        shutil.move(str(item), str(dest))
        print(f"移动: {item} -> {dest}")

    # 删除空的 source_dir
    source_dir.rmdir()
    return True

def main():
    args = parse_arguments()

    # 提取 file_id（文件名不含扩展名）
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 输入文件不存在: {args.input}")
        sys.exit(1)
    file_id = input_path.stem

    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)

    # 确定版本号
    version = get_next_version(output_root, file_id)
    version_dir = output_root / file_id / f"{file_id}_v{version}"
    print(f"文档标识: {file_id}, 新版本: v{version}")
    print(f"版本目录: {version_dir}")

    # 创建临时输出目录（位于同一文件系统，便于快速移动）
    with tempfile.TemporaryDirectory(dir=output_root, prefix=f".tmp_{file_id}_") as temp_dir:
        temp_output_dir = Path(temp_dir)
        print(f"使用临时目录: {temp_output_dir}")

        # 运行 MinerU
        if not run_mineru(args, temp_output_dir):
            print("MinerU 处理失败，终止操作")
            sys.exit(1)

        # 移动生成的内容到版本目录
        if not move_mineru_output_to_version(temp_output_dir, file_id, version_dir):
            print("移动文件失败")
            sys.exit(1)

    print(f"完成！所有 OCR 产物已保存至: {version_dir}")
    # 可选：输出版本目录路径供其他脚本使用
    print(f"VERSION_DIR={version_dir}")

if __name__ == "__main__":
    main()