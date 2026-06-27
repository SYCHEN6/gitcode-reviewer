from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_MODEL: str = "qwen-max"

    GITCODE_TOKEN: str = ""
    GITCODE_BASE_URL: str = "https://gitcode.com"
    WEBHOOK_SECRET: str = ""

    MYSQL_URL: str = ""
    REDIS_URL: str = "redis://localhost:6379"
    ES_URL: str = "http://localhost:9200"

    MCP_SERVER_HOST: str = "localhost"
    MCP_SERVER_PORT: int = 8081


settings = Settings()
