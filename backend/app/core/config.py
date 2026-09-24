from pydantic_settings import BaseSettings, SettingsConfigDict
# 定义配置类
class Settings(BaseSettings):
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    embedding_api_key: str
    embedding_base_url: str
    embedding_model: str
    database_url: str
    redis_url: str
    jwt_secret: str
    rerank_model: str # 重排序模型
    admin_password:str # 管理员密码
    # 指定从哪个文件读取配置,忽略其他变量
    model_config=SettingsConfigDict(env_file=".env",extra="ignore")

settings=Settings() # 实例化配置类,全局单例,别的地方import直接用
