import os
from dotenv import load_dotenv

load_dotenv()

# 读取大模型配置，默认指向 DeepSeek
LLM_API_KEY  = os.getenv("LLM_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL    = os.getenv("LLM_MODEL", "deepseek-chat")
# 语义匹配：与 LLM 共用密钥/基址时可只配 EMBEDDING_MODEL
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", LLM_API_KEY)
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", LLM_BASE_URL)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "")
SEMANTIC_SIMILARITY_THRESHOLD = float(os.getenv("SEMANTIC_SIMILARITY_THRESHOLD", "0.72"))
# 未配置 EMBEDDING_MODEL 时，修正记录检索回退为节点名精确匹配

MONGO_URI     = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "fault-tree-trial")


def _as_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


ENABLE_GRAPH_RETRIEVAL = _as_bool("ENABLE_GRAPH_RETRIEVAL", "false")
ENABLE_GRAPH_TREE_BUILDING = _as_bool("ENABLE_GRAPH_TREE_BUILDING", "true")
GRAPH_TREE_MAX_DEPTH = int(os.getenv("GRAPH_TREE_MAX_DEPTH", "3"))
GRAPH_TREE_MAX_NODES = int(os.getenv("GRAPH_TREE_MAX_NODES", "30"))
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
