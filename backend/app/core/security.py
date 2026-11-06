# Week 4 第六步:JWT签发与校验 
# 本文件干两件事: 
# 1. create_access_token()登录成功后,把"你是admin"这个事实签成token字符串 
# 2. get_current_user FastAPI依赖,挂在受保护router上当门禁,每个请求先过它 
# 为什么单建一个文件:签发和校验是同一把密钥的两端,放一起保证算法/密钥/字段永远一致; 
# 将来要改过期时间、换算法,只用动这一个文件。

import jwt
# PyJWT库(注意:pip包名叫PyJWT,import名叫jwt) 
# 两个核心函数:encode(字典->token字符串)/decode(token字符串->验签->字典)

from datetime import datetime,timedelta,timezone
# datetime:拿当前时间、算过期时间 
# timedelta:表示"一段时长"的对象(比如6小时),专门用于时间加减
# timezone:时区对象,强制使用UTC,避免服务器本地时区不同导致过期时间算错
from jwt.exceptions import ExpiredSignatureError,InvalidTokenError
# ExpiredSignatureError:签名是真的,但过期了——要和"伪造"分开报错,提示前端去重新登录 
# InvalidTokenError:其他一切token问题的父类(签名不符/格式损坏/缺字段)
from fastapi import Depends,HTTPException
from fastapi.security import HTTPBearer,HTTPAuthorizationCredentials
# HTTPBearer:FastAPI内置的"Authorization:Bearer xxx"请求头解析器 
# 它只负责从头里把token字符串抠出来,不做任何校验——校验是我们自己的事 
# 另一个收益:挂上它之后,Swagger UI(docs页面)右上角会出现Authorize按钮,粘贴一次token全局生效 
# HTTPAuthorizationCredentials:抠出来的结果的类型,它的.credentials属性才是token字符串
from app.core.config import settings

ALGORITHM="HS256"
# HS256=HMAC+SHA256,对称签名:同一把jwt_secret既负责签发又负责校验 
# 单服务场景足够;只有"签发方和校验方是不同的服务"时才需要RS256非对称方案

ACCESS_TOKEN_EXPIRE_HOURS=6
# token有效期6小时。手册原则:缓存TTL短(几分钟),JWT长(几小时),别搞混 
# 太短:用户频繁被踢下线;太长:token一旦泄露,坏人能用很久

_bearer=HTTPBearer(auto_error=False)
# auto_error=False:客户端没带Authorization头时,不要自动抛403, 
# 而是把None交给我们,由我们抛语义更准确的401 
# (401=未认证"你是谁";403=已认证但没权限。没带token属于前者)

def create_access_token(subject:str)->str:
    """签发token。只有登录接口会调它。
    subject:写进sub字段的值,标识"这是谁的token"。
    单用户项目固定传"admin";将来加用户表就传user_id。"""
    now=datetime.now(timezone.utc)
    # 为什么强制UTC:服务器将来可能部署到任何时区,UTC是唯一不歧义的时间基准
    # (与PostgreSQL存UTC时间、查询时再转Asia/Shanghai是同一原则)
    payload={
      "sub":subject, # sub(subject):JWT标准注册字段,令牌的归属者
      "iat":now, # iat(issued at):签发时间;PyJWT会自动把datetime转成Unix时间戳
      "exp":now+timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
      # exp(expiration):过期时间。decode时PyJWT自动拿它跟当前时间比,过期就抛ExpiredSignatureError
    }
    return jwt.encode(payload,settings.jwt_secret,algorithm=ALGORITHM)
    # encode内部做的事: 
    # payload字典->JSON->Base64编码->用jwt_secret做HMAC-SHA256签名->拼成xxx.yyy.zzz 
    # 再次提醒:Base64是"编码"不是"加密",任何人都能解开payload看内容——所以token里绝不放敏感信息

async def get_current_user(
  cred:HTTPAuthorizationCredentials|None=Depends(_bearer),
  # Depends(_bearer):FastAPI先跑_bearer解析器,把Authorization头里的token抠出来塞进cred
  # 没带这个头,cred就是None(因为我们设了auto_error=False)
)->str:
  """受保护路由的门禁依赖。
  用法:router=APIRouter(dependencies=[Depends(get_current_user)])
  FastAPI处理该router下每个请求时,先跑本函数:
  通过->路由函数正常执行(返回值没人用,挂在router级别时会被丢弃)
  抛HTTPException->路由根本不会执行,客户端直接收到401
  为什么挂router级别而不是逐个路由加:一行保护整个router,以后新增路由自动继承,不会漏。"""
  if cred is None:
    # 客户端压根没带Authorization头
    raise HTTPException(
      status_code=401,
      detail="未登录,请先调POST/auth/login获取token"
    )
  try:
    payload=jwt.decode(
      cred.credentials, # 抠出来的token字符串
      settings.jwt_secret, # 用同一把secret重算签名做对比
      algorithms=[ALGORITHM], # 必须显式指定算法白名单,防止"alg=none"这类降级攻击
    )
    # decode做三件事:1.验签名 2.查exp是否过期 3.解出payload字典 
    # 任何一步失败都抛异常,绝不可能"解出一个不可信的payload"
  except ExpiredSignatureError:
    # 签名是真的但过期了——提示重新登录,而不是说"你token是假的"
    raise HTTPException(
      status_code=401,
      detail="登录已过期,请重新登录"
    )
  except InvalidTokenError:
    # 伪造/篡改/格式错误,统统归到这里
    raise HTTPException(
      status_code=401,
      detail="token无效"
    )
  sub=payload.get("sub")
  if not sub:
    raise HTTPException(
      status_code=401,
      detail="token无效"
    )
  return sub
  # 返回token归属者。router级别挂载时这个返回值被丢弃
  # 但写成返回sub是为了将来某个路由想知道"当前是谁"时,可以直接在函数参数里Depends拿到

async def get_optional_user(
  cred:HTTPAuthorizationCredentials|None=Depends(_bearer),
) -> str | None:
  """可选登录:带了有效token -> 返回"admin";没带/无效/过期 -> 返回None。
  与 get_current_user 的区别(这是访客模式的核心):
  get_current_user 是"没token就401",适合管理员专用接口;
  get_optional_user 是"没token也放行",适合访客也能用的接口——
  /repos 的提交接口靠它区分"管理员(免配额)"和"访客(走自带Key或拒绝)"。
  为什么参数复用同一个 _bearer?
  它是无状态的解析器,复用它才能让 Swagger 的 Authorize 按钮同时作用于两组依赖。"""
  if cred is None:
    # 没带 Authorization 头 = 纯访客,不是错误
    return None
  try:
    payload=jwt.decode(
      cred.credentials,       # 头里抠出来的 token 字符串
      settings.jwt_secret,    # 同一把密钥做签名校验
      algorithms=[ALGORITHM], # 算法白名单,防"alg=none"降级攻击
    )
  except (ExpiredSignatureError,InvalidTokenError):
    # 过期或伪造:对"可选登录"来说,等价于"没登录",
    # 不抛 401。否则访客拿着一个过期token访问公开接口就完全用不了了
    return None
  sub=payload.get("sub")
  # payload.get 取不到 sub 时返回 None;再 or None 保证返回值类型干净
  return sub or None

