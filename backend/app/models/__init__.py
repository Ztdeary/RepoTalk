from app.models.repo import Repo,RepoStatus # 注册统一出口,别处只认这个入口

__all__=["Repo","RepoStatus"] # 声明公开导出的名字,避免导入所有模块
