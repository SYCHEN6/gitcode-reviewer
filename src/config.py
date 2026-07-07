from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # DashScope（通义 / qwen 系列），用于 Supervisor
    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_MODEL: str = "qwen-max"

    # DeepSeek，用于专家 Agent
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"

    GITCODE_TOKEN: str = ""
    GITCODE_BASE_URL: str = "https://gitcode.com"
    WEBHOOK_SECRET: str = ""

    MYSQL_URL: str = ""
    REDIS_URL: str = "redis://localhost:6379"
    ES_URL: str = "http://localhost:9200"
    EMBEDDING_MODEL: str = "text-embedding-v2"   # DashScope embedding 模型
    EMBEDDING_DIMS:  int = 1536                   # text-embedding-v2 输出维度

    MCP_SERVER_HOST: str = "localhost"
    MCP_SERVER_PORT: int = 8081

    # 并发控制
    MAX_CONCURRENT_REVIEWS: int = 10  # 全局最大并发检视数（单进程）


settings = Settings()
