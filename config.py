import os
from dotenv import load_dotenv

load_dotenv()

# 读取大模型配置，默认指向 DeepSeek
LLM_API_KEY  = os.getenv("LLM_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL    = os.getenv("LLM_MODEL", "deepseek-chat")
# 语义匹配相关配置暂缓启用，先保留注释，当前系统使用节点名称精确匹配。
# EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", LLM_API_KEY)
# EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", LLM_BASE_URL)
# EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "")
# SEMANTIC_SIMILARITY_THRESHOLD = float(
#     os.getenv("SEMANTIC_SIMILARITY_THRESHOLD", "0.72")
# )

MONGO_URI     = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "fault-tree-trial")
