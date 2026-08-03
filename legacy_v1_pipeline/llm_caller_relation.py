# llm_caller_relation.py
import os
import time
import threading
from openai import OpenAI

from env_loader import load_local_env

load_local_env(override=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
OPENAI_MODEL_NAME = os.getenv("OPENAI_MODEL_NAME", "qwen3.5-plus")
LLM_TYPE = os.getenv("LLM_TYPE", "openai")

MAX_TOKENS = int(os.getenv("MAX_TOKENS", "4096"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.1"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "300"))

# === 新增：全局 Token 统计（线程安全）===
_token_lock = threading.Lock()
_total_usage = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
}

def reset_token_usage():
    """重置 token 统计（每个脚本开始时调用）"""
    global _total_usage
    with _token_lock:
        _total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

def get_token_usage():
    """获取当前 token 统计的副本"""
    with _token_lock:
        return _total_usage.copy()
# ================================

def call_openai(prompt, context):
    """调用 OpenAI 标准库，访问阿里云百炼大模型或其他 OpenAI 兼容接口"""
    if not OPENAI_API_KEY or not OPENAI_BASE_URL or not OPENAI_MODEL_NAME:
        raise ValueError("请设置环境变量 OPENAI_API_KEY、OPENAI_BASE_URL 和 OPENAI_MODEL_NAME")

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    messages = [
        {"role": "system", "content": context},
        {"role": "user", "content": prompt}
    ]

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL_NAME,
                messages=messages,
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
                timeout=REQUEST_TIMEOUT,
            )
            # === 新增：累加 token 用量 ===
            usage = response.usage
            if usage:
                with _token_lock:
                    _total_usage["prompt_tokens"] += usage.prompt_tokens
                    _total_usage["completion_tokens"] += usage.completion_tokens
                    _total_usage["total_tokens"] += usage.total_tokens
            # ============================
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"OpenAI 调用失败（尝试 {attempt + 1}/3）: {e}")
            if attempt == 2:
                raise
            time.sleep(2)

def call_llm(prompt, context, mode="entity"):
    """
    调用大语言模型
    :param prompt: 用户提示词
    :param context: 系统上下文
    :param mode: 模式（entity/relation），本实现中未使用，保留兼容性
    :return: 模型返回的文本
    """
    if LLM_TYPE == "openai":
        return call_openai(prompt, context)
    else:
        return "Error: Only 'openai' LLM type is supported (local model support has been removed)"
