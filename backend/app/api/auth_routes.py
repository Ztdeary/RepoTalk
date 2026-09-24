# Week 4 第六步:登录路由 
# 整个项目里唯一"不验token"的业务接口——它就是用来换token的 
# 单用户设计:没有用户表,密码就是.env里的ADMIN_PASSWORD,比对通过就发token

from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field
from app.core.config import settings
from app.core.security import create_access_token,ACCESS_TOKEN_EXPIRE_HOURS

router=APIRouter(
  prefix="/auth", # 本router下所有路由自动加/auth前缀
  tags=["auth"], # Swagger UI分组标签
  # 注意:这里没有dependencies=[Depends(get_current_user)] 
  # 登录接口必须公开,否则"用token换token"就成了死循环
)

class LoginReq(BaseModel):
  """POST/auth/login请求体。"""
  password:str=Field(min_length=1,description="管理员密码,即.env里的ADMIN_PASSWORD")
  
class LoginResp(BaseModel):
  """登录成功响应。字段名access_token/token_type是OAuth2惯例,前端库和Swagger都认这套。"""
  access_token:str # JWT字符串,客户端存起来,后续请求放进Authorization头
  token_type:str="bearer" # 固定值,告诉客户端"Authorization:Bearer <token> "这么拼
  expires_in:int # 有效期秒数,前端拿去算"什么时候该重新登录"

@router.post("/login",response_model=LoginResp,description="管理员登录,密码正确则签发JWT")
async def login(req:LoginReq):
    """POST/auth/login
    校验通过->返回token;失败->401。
    为什么async def:虽然函数体没有await,但保持和其他路由一致的签名风格,
    FastAPI会直接把它丢进事件循环执行,不占用线程池。"""
    if req.password!=settings.admin_password:
      # 单用户场景只有"密码错"一种失败,直接明说
      # (多用户系统才需要"用户名或密码错误"这种模糊提示来防用户枚举)
      raise HTTPException(
        status_code=401,
        detail="密码错误"
      )
    token=create_access_token(subject="admin")
    # sub固定写"admin":单用户项目只有一个身份;将来加用户表时这里改成user_id
    return LoginResp(
      access_token=token,
      expires_in=ACCESS_TOKEN_EXPIRE_HOURS*3600 # 小时->秒,6*3600=21600
    )

