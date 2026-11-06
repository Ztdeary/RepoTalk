from fastapi import FastAPI
from app.api.routes import router
from app.api.repo_routes import router as repo_router # 导入仓库路由
from app.api.auth_routes import router as auth_router # 导入认证路由
from app.api.demo_routes import router as demo_router # 访客模式路由(配置/访问上报/测Key/统计)
from fastapi.responses import JSONResponse # 导入FastAPI的JSON响应类
from contextlib import asynccontextmanager
# contextlib:Python标准库，提供“上下文管理”工具
# asynccontextmanager:装饰器，作用是把一个“带yield的async函数”包装成异步上下文管理器。
from app.core.db import engine,init_db # 导入建表函数+引擎(关闭时要释放)

# 自定义JSON响应类,解决PS5.1乱码问题
class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8" 
    # 显式声明charset,解决PS5.1乱码问题
    # 设置默认响应类为UTF8JSONResponse
    # default_response_class=UTF8JSONResponse让所有路由（包括路由里直接return dict的）都走这个响应类，一处修改全局生效。

@asynccontextmanager 
# 装饰下面的lifespan函数
# 装饰:给函数添加额外功能，不改变原函数，这里给函数添加异步上下文管理器功能
async def lifespan(app:FastAPI):
    """应用启动/关闭钩子。
    为什么不用已废弃的@app.on_event("startup")?
    lifespan是官方推荐写法,而且能在yield之后写清理逻辑(启动+关闭一处搞定)。"""
    await init_db() # 启动时建表(幂等,重复启动无害)
    yield
    # 把函数劈成两半的一刀。执行顺序是:
    # uvicorn启动->执行yield之前的代码(建表);
    # yield这一行暂停,应用开始对外接请求;
    # 进程收到关闭信号(Ctrl+C / reload)->从yield处恢复,执行之后的代码(释放连接池)。
    await engine.dispose() # 关闭时释放连接池,避免开发期反复重启堆积残留连接
    
app = FastAPI(
    title="RepoTalk API",
    default_response_class=UTF8JSONResponse,
    lifespan=lifespan, # 挂上生命周期
)

app.include_router(router)
app.include_router(repo_router)
app.include_router(auth_router)
# 访客模式路由:前缀 /demo,无需登录即可访问 /demo/config、/demo/visit、/demo/test-llm;
# /demo/stats 在路由内部用 Depends(get_current_user) 单独保护,粒度精确到单个接口
app.include_router(demo_router)

@app.get("/health")
async def health():
    """最简健康检查：只要它能返回，说明服务活着。
    所有项目第一步都该有它——让你随时判断 是代码问题还是环境问题。"""
    return {"status": "ok"}
