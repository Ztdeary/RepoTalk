/* 访客身份 + 自带 LLM Key 的唯一存取处:所有请求头都由本文件生成。*/

// 自带 Key 的三要素。前端用 camelCase(驼峰),
// 发请求时由 client.ts 映射成后端要的 base_url/api_key/model
export interface LLMCred {
  baseUrl: string // 服务商地址,如 https://api.deepseek.com
  apiKey: string  // 访客自己的 Key
  model: string   // 模型名,如 deepseek-chat
}

// sessionStorage 的键名:只活在当前标签页,关掉页面就消失,不落盘
// 为什么不用 localStorage 存 Key:localStorage 会一直留在硬盘里,
// 共用电脑时下一个人打开浏览器就能拿到你的 Key;
// sessionStorage 关页即焚,能把"Key 被长期留存"的风险降到最低
const CRED_KEY = 'llm_cred'

// localStorage 的键名:访客 ID 需要长期保留,才能统计"同一个人的多次访问"
const VISITOR_KEY = 'visitor_id'

/* 读取 Key。没配过 / JSON 损坏 / 字段缺失 都返回 null */
export function loadCred(): LLMCred | null {
  // getItem:键不存在时返回 null(而不是抛异常)
  const raw = sessionStorage.getItem(CRED_KEY)
  if (!raw) return null

  try {
    // JSON.parse 直接把字符串转对象;这里先当"可能缺字段"看待
    const obj = JSON.parse(raw) as Partial<LLMCred>
    // 三个字段缺一不可:缺任何一项,拼出来的请求头都会是残废的
    if (!obj.baseUrl || !obj.apiKey || !obj.model) return null
    return { baseUrl: obj.baseUrl, apiKey: obj.apiKey, model: obj.model }
  } catch {
    // 用户手改过 storage / 旧版本格式不兼容:吞掉异常返回 null,
    // 调用方只需判 null,不用写 try/catch
    return null
  }
}

/* 保存 Key(只写 sessionStorage) */
export function saveCred(cred: LLMCred) {
  // storage 只能存字符串,所以对象先 JSON.stringify
  sessionStorage.setItem(CRED_KEY, JSON.stringify(cred))
}

/* 清除 Key(相当于"退出自带 Key 模式",回到只读演示) */
export function clearCred() {
  sessionStorage.removeItem(CRED_KEY)
}

/* 是否已配置 Key:用于按钮文案与"提交仓库"的提示 */
export function hasCred(): boolean {
  return loadCred() !== null
}

/* 取访客 ID:首次访问随机生成并写进 localStorage,之后一直复用同一串 */
export function getVisitorId(): string {
  let id = localStorage.getItem(VISITOR_KEY)
  if (id) return id // 老访客:直接复用,统计才能算"同一个人"

  id = randomId()
  localStorage.setItem(VISITOR_KEY, id)
  return id
}

/* 生成随机 ID。
   坑点:浏览器只在 HTTPS / localhost 这类"安全上下文"里提供 crypto.randomUUID,
   公网 HTTP 下它是 undefined,直接调用会 TypeError。
   所以必须先 typeof 判断再调用,并准备好降级方案。 */
function randomId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  // 降级:时间戳(36进制) + 随机串。
  // Date.now().toString(36) 把毫秒时间压成短串;
  // Math.random().toString(36).slice(2) 切掉开头的 "0."
  return `v-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/* 组装公共请求头:永远带访客 ID,配了 Key 才带三个 X-LLM-* */
export function llmHeaders(): Record<string, string> {
  // Record<string,string>:键和值都是字符串的对象
  const headers: Record<string, string> = { 'X-Visitor-Id': getVisitorId() }

  const cred = loadCred()
  if (cred) {
    // 头名必须和后端 Header(alias="X-LLM-Base-URL") 一字不差,
    // 大小写不敏感但连字符/拼写错了会直接变成"访客没带Key"
    headers['X-LLM-Base-URL'] = cred.baseUrl
    headers['X-LLM-Key'] = cred.apiKey
    headers['X-LLM-Model'] = cred.model
  }
  return headers
}
