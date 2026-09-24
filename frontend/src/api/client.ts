/* 所有后端通信的唯一出口,封装axios实例(自动加JWT头、统一处理401)以及手写SSE流解析函数。*/

// 从axios导入默认导出axios和类型AxiosInstance 
// axios:HTTP客户端库 
// AxiosInstance:axios实例的类型,用于变量的类型标注
import axios, { type AxiosInstance } from 'axios'
// 导入本项目的数据类型定义
import type { Repo } from '../types'
// 顺便把Source/ChatMessage再导出一次,因为ChatPanel.tsx是从../api/client导入这两个类型的
export type { Repo, Source, ChatMessage } from '../types'

// =====创建axios实例 ===== 
// axios.create():创建一个自定义配置的axios实例 
// 好处:所有请求共享同一套配置(baseURL、超时、拦截器),不用每次重复写
const api: AxiosInstance = axios.create({
  // baseURL:所有请求的基础URL 
  // /api:请求/auth/login实际会请求/api/auth/login
  // 配合vite.config.ts的代理,最终转发到后端的/auth/login
  baseURL: '/api',
  // timeout:请求超时时间,30000毫秒=30 秒 
  // 超过30秒还没响应就抛出超时错误,防止前端无限等待
  timeout: 30000,
})

// =====请求拦截器===== 
// api.interceptors.request.use():注册请求拦截器 
// 作用:在每个请求发出之前,统一做一些处理(这里是加认证头) 
// 参数是一个函数,config是请求配置对象
api.interceptors.request.use((config) => {
  // 从localStorage读取 token 
  // localStorage:浏览器本地存储,数据跨页面刷新保留 
  // token是存储时用的键名,登录成功后存进去的
  const token = localStorage.getItem('token')

  // 如果token存在,就加到请求头里
  if (token) {
    // config.headers:请求头对象 
    // Authorization:HTTP认证头 
    // Bearer ${token}:Bearer是OAuth2约定的前缀 
    // 后端security.py按"Bearer <token>"格式解析JWT
    config.headers.Authorization = `Bearer ${token}`
  }
  // 必须return config,否则请求不会发出
  return config
})

// =====响应拦截器===== 
// 第一个参数:2xx成功响应的处理函数,直接放行 
// 第二个参数:错误响应的处理函数
api.interceptors.response.use(
  (response) => response, // 成功:原样返回响应对象
  (error) => {
    // error.config.url 是请求的相对路径(不含 /api,baseURL 是拼在前面的)
    // 登录接口自己的 401 是"密码错了",属于正常业务失败,
    // 不能当成"登录态失效"去清 token + 刷新页面,否则 Login 的红字提示会被刷没
    const isLoginRequest = String(error.config?.url ?? '').includes('/auth/login')

    if (error.response?.status === 401 && !isLoginRequest) {
      // 401=未授权:token无效或过期
      localStorage.removeItem('token')
      // 刷新页面:App组件初始化时发现没有token,会显示登录页
      window.location.reload()
    }
    // Promise.reject(error):把错误继续抛出,让调用方的catch能捕获
    return Promise.reject(error)
  },
)

// =====登录接口===== 
// POST/auth/login 
// 参数:password - 管理员密码 
// 返回:Promise<string> - JWT token字符串
export async function login(password: string): Promise<string> {
  // api.post(url,data):发送POST请求 
  // 第一个参数:URL路径(会拼上baseURL,变成/api/auth/login) 
  // 第二个参数:请求体对象,axios会自动转成JSON
  const res = await api.post('/auth/login', { password })
  // res.data:响应体,后端返回{access_token,token_type,expires_in} 
  // 我们只需要access_token
  return res.data.access_token
}

// =====获取仓库列表===== 
// GET/repos 
// 返回:Promise<Repo[]> - 仓库对象数组
export async function listRepos(): Promise<Repo[]> {
  const res = await api.get('/repos')
  return res.data
}

// =====创建仓库解析任务===== 
// POST/repos 
// 参数: 
// repoUrl - Git仓库HTTPS地址 
// collection - 可选的collection名,不传时后端自动从URL提取 
// 返回:Promise<Repo> - 创建的仓库对象
export async function createRepo(repoUrl: string, collection?: string): Promise<Repo> {
  // {repo_url:repoUrl,collection}:请求体 
  // 注意字段名是repo_url(下划线),因为后端Pydantic模型用的是repo_url
  const res = await api.post('/repos', { repo_url: repoUrl, collection })
  return res.data
}

// =====查询单个仓库状态===== 
// GET/repos/{id} 
// 参数:repoId - 仓库ID 
// 返回:Promise<Repo> - 仓库对象
export async function getRepo(repoId: number): Promise<Repo> {
  // 模板字符串:把repoId拼进URL
  const res = await api.get(`/repos/${repoId}`)
  return res.data
}

// =====SSE请求封装 ===== 
// 为什么不用浏览器原生的EventSource? 
// EventSource不支持自定义请求头,也就无法携带Authorization:Bearer<token> 
// 而我们所有接口都需要JWT认证 
// 所以用fetch发请求,再手动按SSE协议解析响应流

// SSE回调接口:定义调用方可以注册哪些回调函数
export interface SSECallbacks {
  // onMessage:收到一条完整SSE消息时调用 
  // 参数data:消息内容字符串(后端发的JSON字符串)
  onMessage: (data: string) => void //void表示没有返回值,只负责处理消息
  // onError:连接出错时调用(可选)
  onError?: (error: Error) => void
  // onClose:连接正常关闭时调用(可选)
  onClose?: () => void
}

/**
 * 通用SSE请求函数
 * @param url - 请求路径(不含/api前缀,如/repos/5/events)
 * @param callbacks - 回调集合
 * @param body - 可选的请求体,传了就用POST,不传就用GET
 * @returns abort 函数,调用它可以主动中断连接
 */
export function sseRequest(
  url: string,
  callbacks: SSECallbacks,
  body?: Record<string, unknown>
  // Record<string,unknown>:表示"键是字符串、值是任意类型"的对象
): () => void {
  // AbortController:浏览器原生的"取消请求"控制器 
  // controller.abort()可以中断正在进行的fetch请求
  const controller = new AbortController()

  // 从localStorage读取token
  const token = localStorage.getItem('token')

  // fetch:浏览器原生的HTTP请求API 
  // 第一个参数:URL 
  // 第二个参数:配置对象
  fetch(`/api${url}`, {
    // method:HTTP方法,有body就是POST,没有就是GET
    method: body ? 'POST' : 'GET',
    // headers:请求头
    headers: {
      'Content-Type': 'application/json', // 告诉后端请求体是JSON格式
      // 展开运算符...:有token就加Authorization头,没有就不加
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    // body:请求体,POST时传,JSON.stringify把对象转成JSON字符串
    body: body ? JSON.stringify(body) : undefined,
    // signal:把abort信号传给fetch 
    // 调用controller.abort()时,fetch会抛出AbortError
    signal: controller.signal,
  })
    // .then():fetch返回Promise,用then处理响应
    .then(async (response) => {
      // 先判 HTTP 状态码:fetch 只在网络层失败才 reject,
      // 401(token过期)/500(后端崩)都会正常 resolve,但 body 不是 SSE 数据
      if (!response.ok) {
        // 401 与 axios 拦截器保持同一套行为:清 token + 回登录页
        if (response.status === 401) {
          localStorage.removeItem('token')
          window.location.reload()
        }
        // 其他状态交给上层 onError 显示错误,避免"空白回答"
        callbacks.onError?.(new Error(`HTTP ${response.status}`))
        return
      }
      // response.body:响应体的ReadableStream(可读流) 
      // getReader():获取流的读取器
      const reader = response.body?.getReader()
      if (!reader) {
        // 流不可读,调用错误回调
        callbacks.onError?.(new Error('流不可读'))
        return
      }

      // TextDecoder:把二进制数据(Uint8Array)解码成字符串 
      // utf-8:指定编码,因为后端返回的是UTF-8中文
      const decoder = new TextDecoder('utf-8')

      // buffer:累积缓冲区 
      // 因为流是分块到达的,一块可能包含半条消息,需要攒起来
      let buffer = ''

      // 循环读取流的每一块
      while (true) {
        // reader.read():读取一块数据 
        // 返回{done:boolean,value:Uint8Array} 
        // done=true表示流结束;value是这一块的二进制数据
        const { done, value } = await reader.read()
        if (done) break // 流结束,退出循环

        // decoder.decode(value,{stream:true}):把二进制解码成字符串 
        // stream:true:告诉解码器还有后续数据,不要强制解码多字节字符的中间状态 
        // 例:一个中文字符占3字节,如果这块只收到2字节,stream:true会保留这2字节等下一块
        buffer += decoder.decode(value, { stream: true })

        // SSE协议:每条消息以\n\n(两个换行)分隔 
        // 消息格式:data:{json字符串}\n\n 
        // 按换行符切分行
        const lines = buffer.split('\n')
        // 最后一行可能是不完整的(下一块才会补全),放回buffer
        buffer = lines.pop() || '' // pop()移除数组最后一个元素,把不完整的最后一行去掉

        // 遍历每一行
        for (const line of lines) {
          // SSE 消息以data:开头
          if (line.startsWith('data:')) {
            // line.slice(5):去掉前5个字符'data:' 
            // .trim():去掉首尾空格和换行
            const data = line.slice(5).trim()
            if (data) {
              // 调用消息回调,把数据传给调用方
              callbacks.onMessage(data)
            }
          }
          // 以冒号':'开头的行是SSE注释(心跳包),浏览器自动忽略,我们也跳过
        }
      }
      // 流正常结束,调用关闭回调
      callbacks.onClose?.()
    })
    // .catch():捕获fetch过程中的错误
    .catch((error) => {
      // AbortError是主动调用abort()触发的,不算错误,不调用onError
      if (error.name !== 'AbortError') { // 只有不是AbortError才调用onError
        callbacks.onError?.(error)
      }
    })

  // 返回一个函数,调用方执行它就能中断请求
  return () => controller.abort()
}


