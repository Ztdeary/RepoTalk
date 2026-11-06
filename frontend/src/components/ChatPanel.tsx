/* 聊天面板组件,是本层最核心的组件,通过SSE流式接收status/sources/token,逐字渲染回答并用shiki做代码高亮。*/

/* ============================================================
 * ChatPanel.tsx——聊天面板:流式问答的主战场
 * 在项目中的作用:
 * 仓库解析完成(stage==='done')后由RepoDetail挂载,用户在这里提问:
 * 1) 把问题POST给后端/chat/stream(请求自动带JWT认证头)
 * 2) 按SSE消息的type字段分发处理:
 *    status  ->显示"正在检索资料..."
 *    sources ->渲染引用来源卡片
 *    token   ->逐字拼接到AI回答里(打字机效果)
 *    done    ->固化回答,代码块切换成shiki高亮渲染
 *    error   ->红字显示错误信息
 * 3) 用react-markdown渲染Markdown回答,代码块交给shiki做语法高亮
 * ============================================================ */

// useState:声明组件状态(值变了自动重渲染) 
// useEffect:处理副作用(订阅、清定时器这类"渲染之外"的事) 
// useRef:保存"变了也不触发重渲染"的可变值(中断函数、DOM锚点)
import { useState, useEffect, useRef, type KeyboardEvent } from 'react'
// ReactMarkdown:把Markdown格式的纯文本字符串(AI的回答)渲染成带标题、列表、代码块等排版效果的React元素树
// AI的回答是Markdown格式(标题/列表/代码块),当纯文本显示会糊成一团
import ReactMarkdown from 'react-markdown'
// Components:react-markdown暴露的类型,描述"自定义渲染器映射表"的形状
import type { Components } from 'react-markdown'
// remark-gfm:GitHub风格Markdown插件,让ReactMarkdown支持表格、删除线、任务列表
import remarkGfm from 'remark-gfm'
// codeToHtml:shiki的便捷函数——输入代码字符串,输出带语法配色的HTML字符串 
// 底层用的是VS Code同款TextMate语法引擎,高亮质量比正则方案高一个档次
import { codeToHtml } from 'shiki'
// sseRequest:../api/client.ts里手写的SSE客户端(fetch+ReadableStream) 
// 不用EventSource的原因:它不能自定义请求头,带不了JWT 
// type ChatMessage/type Source:消息与引用来源的类型定义 
// (client.ts第10行从types.ts再导出了这两个类型,所以从这里导入)
import {
  sseRequest,
  getSiteConfig,
  listChatSessions,
  getChatSession,
  createChatSession,
  updateChatSession,
  deleteChatSession,
  type ChatMessage,
  type Source,
  type SiteConfig,
  type ChatSessionSummary,
  type SavedChatMessage,
} from '../api/client'

// =====Props接口:与RepoDetail第181行<ChatPanel repoId={repoId}/>对齐=====
interface ChatPanelProps {
  // repoId:当前仓库id 
  // 用途:POST/chat/stream时放进请求体,后端靠它查repos表得到collection名再做检索
  repoId: number
  // repoUrl:仓库的GitHub地址(如https://github.com/psf/requests)
  // 用途:引用卡片上的"跳转"链接靠它拼出完整GitHub文件URL
  // 可选(?:):详情接口还没回来时可能为空,此时不渲染跳转链接
  repoUrl?: string
}

// =====SSE消息类型:与后端token_stream生成器推的5种消息一一对应===== 
// 后端每条消息的JSON里都有type字段,其余字段按类型可有可无
interface StreamMsg {
  // type:消息类型,只能是这5个字符串之一
  type: 'status' | 'sources' | 'token' | 'done' | 'error'
  // sources:引用来源数组,仅type==='sources'时有值
  sources?: Source[]
  // content:一个token文本片段,仅type==='token'时有值
  content?: string
  // message:错误描述,仅type==='error'时有值
  message?: string
}

// =====GitHub跳转链接构造函数=====
// 输入:仓库地址+一条引用来源(含文件路径和行号区间)
// 输出:https://github.com/psf/requests/blob/HEAD/requests/models.py#L120-L155
// 打开效果:浏览器新标签直达该文件,并自动高亮、滚动到引用的那段代码
function buildGithubUrl(repoUrl: string, s: Source): string {
  // 先削掉所有结尾斜杠(/\/+/支持多个),再削.git
  // 顺序不能反:反过来时"...git/"这种"clone地址+尾斜杠"的写法会漏网
  const base = repoUrl.replace(/\/+$/, '').replace(/\.git$/, '')  

  // replace(/\\/g,'/'):把反斜杠全换成正斜杠
  // 为什么要换:worker跑在Windows上时,切分器算出的相对路径是反斜杠(src\cli.py),
  // 而GitHub的URL只认正斜杠;g标志=全局替换,不只换第一个
  const path = s.file_path.replace(/\\/g, '/')
  // blob/HEAD:HEAD是Git里"当前默认分支"的别名
  // 为什么不写死main:老仓库默认分支可能叫master,写死main会404;
  // GitHub遇到/blob/HEAD/会自动重定向到真实默认分支,两边通吃
  // #L120-L155:GitHub的行号锚点语法,L=Line,浏览器打开后自动高亮这段区间
  return `${base}/blob/HEAD/${path}#L${s.start_line}-L${s.end_line}`
}

// =====把前端消息压缩成"可保存"的格式=====
// 过滤规则:
//   1) 只保留有内容的(user 或 assistant 都要 content 非空);
//   2) assistant 必须已结束(done=true),避免把"生成到一半"的回答存进历史。
function buildSavedMessages(list: ChatMessage[]): SavedChatMessage[] {
  return list
    .filter((m) => m.content.trim() !== '')
    .filter((m) => m.role === 'user' || !!m.done)
    .map((m) => ({
      role: m.role,
      content: m.content,
      // sources/error 都原样带上:重进会话时引用卡片、错误提示都能恢复
      sources: m.sources,
      error: m.error,
    }))
}

// =====历史会话时间展示=====
// 只显示"月-日 时:分",列表够用;完整时间对浏览历史没有帮助还占宽度
function formatSessionTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

// =====代码块组件:把一段代码用shiki高亮成带配色的HTML===== 
// 为什么单独抽成组件:高亮是异步操作(codeToHtml返回Promise)
// 需要用useState+useEffect包一层,这套逻辑必须待在组件里
function CodeBlock({ lang, code }: { lang: string, code: string }) {
  // html:shiko产出的HTML字符串。空串=还没高亮完(首次渲染必然如此)
  const [html, setHtml] = useState('')

  // 副作用:code或lang变化时重新高亮 
  // 对已完成的消息code固定不变,整个生命周期只高亮一次,性能无忧
  useEffect(() => {
    // mounted:组件存活标志。高亮是异步的,完成时组件可能已卸载
    // 卸载后再setState会触发React警告,用这个布尔值挡一道
    let mounted = true

    // codeToHtml(code,{lang,theme}):把代码按lang语言、github-dark配色高亮
    codeToHtml(code, { lang, theme: 'github-dark' })
      .then((result) => {
        if (mounted) setHtml(result)
      })
      .catch(() => {
        // shiki不认识的语言(如protobuf)会抛错 
        // 降级方案:按'text'纯文本重新高亮(无色但至少排版正确)
        codeToHtml(code, { lang: 'text', theme: 'github-dark' })
          .then((result) => {
            if (mounted) setHtml(result)
          })
          .catch(() => {
            // text都失败(几乎不可能):放弃,页面保持下方的无色占位
          })
      })

    // 清理函数:组件卸载或code/lang变化时落闸,挡住迟到的异步回调
    return () => {
      mounted = false
    }
  }, [code, lang]) // 依赖数组:这两个值变才重跑

  // 高亮未完成:先渲染无色占位,避免代码区域空白闪烁
  if (!html) {
    return (
      <pre className="code-block">
        <code>{code}</code>
      </pre>
    )
  }

  // dangerouslySetInnerHTML:React默认把一切HTML当文本转义, 
  // 而shiki产出的是可信的纯样式标签(只有<span style>),必须原样注入 
  // __html:固定的属性名,双下划线是React刻意设计的"危险提醒"
  // 作用:把shiki生成的高亮HTML原样注入DOM,避免React把标签当文本转义,从而让代码块呈现出带配色的高亮效果。
  return <div className="code-block" dangerouslySetInnerHTML={{ __html: html }} />
}

// =====回答完成后的Markdown渲染器映射表===== 
// Components类型:{标签名:自定义渲染函数} 
// 为什么不全程用这套高亮渲染器:流式期间每来一个token整篇Markdown都重渲染
// 代码块每token重跑一次shiki高亮会把主线程卡死 
// 策略:生成中用react-markdown默认渲染(朴素pre),done那一刻才换上这套高亮版
const highlightComponents: Components = {
  // pre:剥掉react-markdown默认的<pre>包裹层 
  // 不剥的话我们的CodeBlock会被再包一层<pre>,嵌套<pre>破坏排版 
  // <>{children}</>:React空标签(Fragment),意思是"只留孩子,不要外壳"
  pre: ({ children }) => <>{children}</>,

  // code:决定每个代码片段怎么渲染 
  // className:代码块的类名,形如"language-python";children:代码文本
  code({ className, children }) {
    // /language-(\w+)/:正则抠出语言名 
    // (\w+)是捕获组:匹配一个或多个字母数字下划线,括号里的内容进match[1] 
    // exec:执行匹配,成功返回数组,失败返回null
    const match = /language-(\w+)/.exec(className || '')

    // String(children ?? ''):children是ReactNode类型,强制转字符串 
    // ?? '':children为null/undefined时用空串兜底 
    // .replace(/\n$/,''):Markdown代码块结尾自带一个换行,去掉防止高亮后多一行空白
    const text = String(children ?? '').replace(/\n$/, '')

    if (!match) {
      // 无语言标记的两种情况:
      if (text.includes('\n')) {
        // 含换行=没写语言的代码块,给朴素<pre>保持块级排版
        return (
          <pre className="code-block">
            <code>{text}</code>
          </pre>
        )
      }
      // 不含换行=行内代码(如`foo`),给<code>保持行内显示
      return <code className={className}>{children}</code>
    }

    // 有语言标记:交给CodeBlock做shiki高亮 
    // match[1]:正则第一个捕获组,即语言名(如"python")
    return <CodeBlock lang={match[1]} code={text} />
  },
}

// export default:默认导出,RepoDetail里import ChatPanel拿到的就是它
export default function ChatPanel({ repoId, repoUrl }: ChatPanelProps) {
  // =====五个状态/引用=====

  // messages:整条对话记录(用户消息+AI消息按时间顺序排列)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  // input:输入框当前文本。受控组件模式:输入框显示什么完全由这个状态决定
  const [input, setInput] = useState('')
  // loading:true=一次问答正在进行 
  // 用途:禁用发送按钮,保证同一时刻只有一条在途SSE流
  const [loading, setLoading] = useState(false)
  // statusText:检索阶段的提示文案("正在检索资料...") 
  // 空串=不显示。后端缓存命中时不推status消息,这里就一直保持空串
  const [statusText, setStatusText] = useState('')
  // siteCfg:站点配置,用于显示"每日额度/是否可配Key"的提示文案。
  // 拿不到就不显示提示,不影响提问本身
  const [siteCfg, setSiteCfg] = useState<SiteConfig | null>(null)
  // =====历史会话状态(左侧栏)=====
  // sessions:当前仓库+当前访客的历史会话,后端已按 updated_at 倒序(最新在上)
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([])
  // sessionLoading:左侧栏"加载中"占位,避免先闪一下"暂无历史"
  const [sessionLoading, setSessionLoading] = useState(true)
  // activeSessionId:当前正在查看的是哪条历史会话;null=新对话
  // 用途:①列表高亮 ②退出时决定"更新旧会话"还是"新建会话"
  const [activeSessionId, setActiveSessionId] = useState<number | null>(null)
  // confirmDelId:待二次确认删除的会话 id(删除不可恢复,和仓库删除保持一致的交互)
  const [confirmDelId, setConfirmDelId] = useState<number | null>(null)
  // deletingId:正在删除的会话 id,用来禁用按钮防连点
  const [deletingId, setDeletingId] = useState<number | null>(null)
  // historyError:历史会话操作失败的红字(加载/删除)
  const [historyError, setHistoryError] = useState('')
  // abortRef:保存当前SSE连接的中断函数 
  // 用useRef而不用useState:中断函数的变化不需要触发重渲染
  const abortRef = useRef<(() => void) | null>(null)
  // bottomRef:消息列表末尾的DOM锚点,自动滚动用 
  // HTMLDivElement|null:初始null(还没挂载),挂载后指向那个div
  // HTMLDivElement是TypeScript中<div>元素的类型,它告诉useRef这个ref将来会指向一个div的DOM对象
  const bottomRef = useRef<HTMLDivElement | null>(null)
  // messagesRef:永远指向最新的 messages。
  // 为什么需要:退出保存发生在 useEffect 的清理函数里,闭包拿到的是旧的 messages 状态,
  // 用 ref 才能拿到最后一刻的完整对话(包括刚流式结束的那条回答)
  const messagesRef = useRef<ChatMessage[]>([])
  // activeSessionRef:同上,退出时要判断"更新哪条旧会话",避免闭包读到旧 state
  const activeSessionRef = useRef<number | null>(null)
  // loadSeqRef:历史会话加载序号,防止快速点 A 再点 B 时,A 的慢响应把 B 覆盖掉
  const loadSeqRef = useRef(0)

  // =====副作用1:repoId变化=用户切了仓库,清空对话从头开始===== 
  // 必须清的原因:上个仓库的问答记录对新仓库毫无意义
  // 且上个仓库没聊完的流还挂着,掐掉防止连接泄漏
  useEffect(() => {
    abortRef.current?.()
    // ?.:可选链——还没建立过连接时是null,不崩
    // current的作用:指向最新值,确保在组件卸载时调用的是最新中断函数
    setMessages([]) // 清聊天记录
    setInput('') // 清输入框
    setStatusText('') // 清检索提示
    setLoading(false) // 解锁发送按钮
    setActiveSessionId(null) // 切仓库后不再指向上一个仓库的历史会话
    setConfirmDelId(null)
    setHistoryError('')
  }, [repoId]) // 依赖:repoId变就重跑(首次挂载也会跑一次,无副作用)

  // =====副作用1.1:两个 ref 与 state 同步=====
  // 为什么不直接在渲染里赋值:渲染函数可能被 React 丢弃/重跑,
  // 放进 effect 才能保证"提交后的值"和真实状态一致
  useEffect(() => {
    messagesRef.current = messages
  }, [messages])
  useEffect(() => {
    activeSessionRef.current = activeSessionId
  }, [activeSessionId])

  // =====副作用1.2:进入/切换仓库时拉取历史会话列表=====
  useEffect(() => {
    let alive = true // 防止组件卸载后 setState 触发警告
    setSessionLoading(true)
    listChatSessions(repoId)
      .then((data) => {
        if (alive) setSessions(data)
      })
      .catch(() => {
        // 拉不到历史只影响左侧栏,问答主流程照常;清空避免显示旧仓库的会话
        if (alive) setSessions([])
      })
      .finally(() => {
        if (alive) setSessionLoading(false)
      })
    return () => {
      alive = false
    }
  }, [repoId])

  // =====副作用1.3:退出问答页时自动保存本次会话=====
  // 触发时机:SPA 里点「返回列表」导致 ChatPanel 卸载。
  // 规则:
  //   1) 没有任何有效消息(只打了字没发送/流没产出内容)就不保存;
  //   2) 当前是打开的历史会话 -> PUT 更新原会话;
  //   3) 否则 -> POST 新建会话,标题由后端 LLM 总结。
  // 这里用 fire-and-forget:保存失败不能阻塞用户返回列表,下一次聊天还能再存。
  useEffect(() => {
    return () => {
      const payload = buildSavedMessages(messagesRef.current)
      if (payload.length === 0) return
      const sid = activeSessionRef.current
      if (sid) {
        updateChatSession(sid, payload).catch(() => {})
      } else {
        createChatSession(repoId, payload).catch(() => {})
      }
    }
  }, [repoId])

  // =====副作用2:组件卸载时掐断在途SSE流===== 
  // 不做会怎样:用户返回列表页后流还挂着,后台一直收token,白烧资源
  useEffect(() => {
    // useEffect的返回值是清理函数,组件卸载时执行
    return () => {
      abortRef.current?.()
    }
  }, []) // 空依赖:只在挂载/卸载时执行一次

  // =====副作用3:挂载时读一次站点配置(额度提示用)=====
  // 只读一次即可:额度是会话期间的静态配置,不需要实时刷新;
  // 失败就保持 null,界面上不显示提示,但提问流程照常
  useEffect(() => {
    getSiteConfig().then(setSiteCfg).catch(() => setSiteCfg(null))
  }, [])

  // =====副作用4:messages每变一次,滚动条压到底部===== 
  // 流式期间token疯狂追加,每次追加都压底,用户始终看到最新内容
  useEffect(() => {
    // scrollIntoView():把这个DOM元素滚进可视区域
    // 不传behavior:'smooth'的原因:token每秒来几十个
    // 平滑滚动动画会不断被打断重启动,视觉上抖得厉害,瞬时滚动反而稳
    bottomRef.current?.scrollIntoView()
  }, [messages])

  // =====工具函数1:按id局部更新某条AI消息的字段===== 
  // Partial<ChatMessage>:把ChatMessage所有字段变可选——传哪些就更新哪些 
  // 为什么必须函数式更新:setMessages((prev)=>...) 
  // onMessage回调是异步的,闭包里的messages是旧值, 
  // prev才是React保证的最新状态
  const patchAssistant = (id: string, patch: Partial<ChatMessage>) => {
    setMessages((prev) =>
      // map:遍历每条消息,id匹配的就展开旧字段再覆盖patch里的字段,不匹配的原样保留
      prev.map((m) => (m.id === id ? { ...m, ...patch } : m)),
    )
  }

  // =====工具函数2:给一个token拼接到AI消息末尾===== 
  // 为什么单独抽出来:token是"拼接"不是"覆盖"
  // 与patchAssistant的语义不同,混用会把之前的内容冲掉
  const appendToken = (id: string, token: string) => {
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, content: m.content + token } : m)),
    )
  }

  // =====核心动作:发送问题,建立SSE流=====
  const send = () => {
    // trim():去掉首尾空白,全是空格的问题等于没问
    const question = input.trim()
    // 双闸门:空问题不送;上一次没聊完不送(loading=true时按钮也已禁用,这里是双保险)
    if (!question || loading) return

    // 消息唯一ID:时间戳(36进制)+随机串,撞车概率可忽略 
    // Date.now().toString(36):毫秒时间戳转36进制字符串 
    // Math.random().toString(36).slice(2):随机数的36进制,切掉开头的"0."
    const userId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
    const assistantId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`

    // 一次性把「用户消息+AI占位消息」追加进列表 
    // AI占位消息:content空串、done=false,后续token来了往里填,done到了固化
    setMessages((prev) => [
      ...prev,
      { id: userId, role: 'user', content: question },
      { id: assistantId, role: 'assistant', content: '', done: false },
    ])
    setInput('') // 清空输入框
    setLoading(true) // 上锁:流没结束不许再发
    setStatusText('') // 清掉上一次的残留提示

    // 发起SSE请求。sseRequest三个参数:URL、回调集合、请求体(有body=POST)
    // 返回值是中断函数,存进ref供卸载/切仓库时掐流
    abortRef.current = sseRequest(
      '/chat/stream',
      {
        // onMessage:每收到一条完整SSE消息调一次 
        // data:消息里的JSON字符串(后端json.dumps的产物)
        onMessage: (data) => {
          // JSON.parse:字符串->JS对象;as StreamMsg:告诉TS按我们的类型看
          const msg = JSON.parse(data) as StreamMsg

          // 按type字段分发,5种消息5条路
          if (msg.type === 'status') {
            // 检索开始(只有缓存未命中时后端才推这条)
            setStatusText('正在检索资料...')
          } else if (msg.type === 'sources') {
            // 检索结束:引用列表到了
            setStatusText('') // 清掉"检索中"提示
            // ?? []:sources万一缺失用空数组兜底,防止渲染层崩
            patchAssistant(assistantId, { sources: msg.sources ?? [] })
          } else if (msg.type === 'token') {
            // 逐字拼接:打字机效果的核心
            // 后端LLM每吐一个词推一条,这里实时拼到AI消息末尾
            appendToken(assistantId, msg.content ?? '')
          } else if (msg.type === 'done') {
            // 正常结束:固化回答
            // done=true会触发渲染层切换到shiki高亮版Markdown
            patchAssistant(assistantId, { done: true })
            setLoading(false) // 解锁,可以问下一个问题了
          } else if (msg.type === 'error') {
            // 后端在流内推的错误(检索失败/生成中断)
            // 已收到的部分内容保留,只追加错误提示
            patchAssistant(assistantId, { done: true, error: msg.message ?? '未知错误' })
            setLoading(false)
            setStatusText('')
          }
        },

        // onError:连接层错误(断网/401/服务器崩),与流内error消息是两条路
        onError: (err) => {
          patchAssistant(assistantId, {
            done: true,
            error: `连接中断:${err.message}`,
          })
          setLoading(false)
        },

        // onClose:流正常关闭(通常在done之后) 
        // 兜底作用:服务器万一没发done就断流
        // 强制固化消息,不让光标永远闪、按钮永远锁
        onClose: () => {
          patchAssistant(assistantId, { done: true })
          setLoading(false)
        },
      },
      // 请求体:repo_id+question。后端ChatReq模型用repo_id查collection再检索
      { repo_id: repoId, question },
    )
  }

  // =====打开一条历史会话:把消息回填到聊天面板=====
  const openSession = async (sessionId: number) => {
    const seq = ++loadSeqRef.current // 本次请求的序号,用于丢弃过期响应
    setHistoryError('')
    try {
      const detail = await getChatSession(sessionId)
      if (seq !== loadSeqRef.current) return // 期间用户又点了别的会话,本次结果作废
      // 打开历史=切换对话,先把可能还在跑的流掐掉,避免旧回答继续写进新会话
      abortRef.current?.()
      setLoading(false)
      setStatusText('')
      setInput('')
      setMessages(
        detail.messages.map((m, i) => ({
          id: `hist-${sessionId}-${i}`, // 历史消息没有实时 id,用序号造一个稳定的
          role: m.role,
          content: m.content,
          sources: m.sources,
          error: m.error,
          done: true, // 已存下来的回答都是完成的,done=true 才会走高亮渲染
        })),
      )
      setActiveSessionId(sessionId)
      setConfirmDelId(null)
    } catch (err: any) {
      if (seq === loadSeqRef.current) {
        setHistoryError(err.response?.data?.detail || '加载历史会话失败')
      }
    }
  }

  // =====删除一条历史会话=====
  const removeSession = async (sessionId: number) => {
    setDeletingId(sessionId)
    setHistoryError('')
    try {
      await deleteChatSession(sessionId)
      setSessions((prev) => prev.filter((s) => s.id !== sessionId))
      // 删的正好是当前打开的会话:把面板清空并回到"新对话"状态,
      // 否则用户还能在同一份内容上继续提问,退出时又会把它保存回来
      if (activeSessionRef.current === sessionId) {
        setMessages([])
        setActiveSessionId(null)
        setInput('')
        setStatusText('')
      }
      setConfirmDelId(null)
    } catch (err: any) {
      setHistoryError(err.response?.data?.detail || '删除会话失败')
      setConfirmDelId(null)
    } finally {
      setDeletingId(null)
    }
  }

  // =====键盘事件:Enter发送,Shift+Enter换行(聊天软件通用约定)=====
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // e.nativeEvent.isComposing:中文输入法组词状态标志
    // 拼音打到一半按Enter是"选字上屏"不是"发送",必须放行
    // 不加这个判断,中文用户每选一个字就发一次消息
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault() // 阻止默认行为(换行),否则发送后输入框还会多一个空行
      send()
    }
  }

  // =====JSX渲染=====
  return (
    <section className="chat-layout">
      {/* =====左侧:历史会话栏=====
          为什么放在 ChatPanel 内部而不是 RepoDetail:
          会话数据、当前激活项、退出保存逻辑都在这里,拆出去要额外传一堆状态 */}
      <aside className="chat-history">
        <div className="chat-history-head">
          <h3>历史会话</h3>
        </div>

        {/* 加载/删除失败的红字;成功操作会立刻消失 */}
        {historyError && <div className="error">{historyError}</div>}

        {/* 三态互斥:加载中 / 空 / 有数据 */}
        {sessionLoading ? (
          <p className="hint">加载中...</p>
        ) : sessions.length === 0 ? (
          <p className="hint">暂无历史会话,退出问答后会自动保存</p>
        ) : (
          <ul className="chat-history-list">
            {sessions.map((s) => (
              <li
                key={s.id}
                // active:高亮当前正在查看的会话,让用户知道自己在看哪一段
                className={`chat-history-item${activeSessionId === s.id ? ' active' : ''}`}
                onClick={() => openSession(s.id)}
              >
                <div className="chat-history-main">
                  {/* title 属性:标题被 CSS 省略号截断时,悬停能看全 */}
                  <div className="chat-history-title" title={s.title}>
                    {s.title}
                  </div>
                  <div className="chat-history-time">
                    {formatSessionTime(s.updated_at)} · {s.message_count} 条
                  </div>
                </div>

                {/* 操作区要 stopPropagation,否则点删除会同时触发"打开会话" */}
                <div className="chat-history-actions" onClick={(e) => e.stopPropagation()}>
                  {confirmDelId === s.id ? (
                    <>
                      <button
                        className="chat-history-del-confirm"
                        disabled={deletingId === s.id}
                        onClick={() => removeSession(s.id)}
                      >
                        {deletingId === s.id ? '删除中' : '确认'}
                      </button>
                      <button
                        className="chat-history-cancel"
                        disabled={deletingId === s.id}
                        onClick={() => setConfirmDelId(null)}
                      >
                        取消
                      </button>
                    </>
                  ) : (
                    <button
                      className="chat-history-del"
                      title="删除该历史会话"
                      onClick={() => setConfirmDelId(s.id)}
                    >
                      删除
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <section className="chat-panel">
      {/* =====消息列表区===== */}
      <div className="messages">
        {/* 空对话时的引导语。&&短路:有消息就不渲染 */}
        {messages.length === 0 && (
          <p className="hint">试着问:「这个项目的入口在哪里?」「某个函数是怎么实现的?」</p>
        )}

        {/* map遍历消息列表,一条消息渲染一个气泡 */}
        {messages.map((m) => (
          // key:React diff算法识别列表元素的凭据,必须用稳定唯一ID
          // 模板字符串拼类名:"message user"或"message assistant",CSS靠它分左右配色
          <div key={m.id} className={`message ${m.role}`}>
            {/* 角色标签:三目运算符,用户消息显示"你",AI消息显示"AI" */}
            <div className="message-role">{m.role === 'user' ? '你' : 'AI'}</div>

            <div className="message-body">
              {m.role === 'user' ? (
                // 用户消息:纯文本直接显示,React自动转义HTML,安全
                // 换行靠CSS的white-space:pre-wrap保留(index.css里配)
                <div className="plain-text">{m.content}</div>
              ) : (
                // AI消息:sources卡片+Markdown正文+光标+错误,共四块
                // <> ... </> :Fragment空标签,把多个元素打包成一个返回,不产生多余DOM
                <>
                  {/* 引用来源卡片:有sources才渲染 */}
                  {m.sources && m.sources.length > 0 && (
                    <div className="sources">
                      <p className="sources-title">引用来源({m.sources.length})</p>
                      {m.sources.map((s) => (
                        // details/summary:浏览器原生折叠面板,点击展开/收起,零JS实现
                        // key用file_path:start_line拼接——同一仓库内这个组合天然唯一
                        <details
                          key={`${s.file_path}:${s.start_line}`}
                          className="source-card"
                        >
                          <summary>
                            {/* 左侧:文件位置文本,包一层span方便CSS做左右布局 */}
                            <span className="source-path">
                              {s.file_path}:{s.start_line}-{s.end_line}
                              {s.symbol ? `(${s.symbol})` : ''}
                            </span>

                            {/* 右侧:跳转链接,新标签打开GitHub对应文件+行号高亮 */}
                            {/* repoUrl && (...):repoUrl为空(详情接口还没回来)时整块不渲染,
                              否则会拼出/blob/HEAD/...这种相对路径,点了是404死链。
                              && 短路后 TS 也顺便把 repoUrl 收窄成 string,不会报类型错 */}
                            {repoUrl && (
                              <a
                                className="source-link"
                                href={buildGithubUrl(repoUrl, s)}
                                // target="_blank":新标签页打开,不把当前聊天页面刷掉
                                target="_blank"
                                // rel="noreferrer":安全加固——阻止新页面通过window.opener
                                // 反向操控我们的页面(target="_blank"的经典漏洞,必须配)
                                rel="noreferrer"
                                // onClick里stopPropagation:阻止点击事件冒泡
                                // 为什么必须加:这个<a>长在<summary>里面,而点击summary的默认行为
                                // 是"折叠/展开details"——不拦截的话,点链接会同时触发跳转+折叠,
                                // 卡片莫名其妙合上,体验很奇怪
                                onClick={(e) => e.stopPropagation()}
                              >
                                GitHub ↗
                              </a>
                            )}
                          </summary>
                          <pre className="source-preview">{s.content}</pre>
                        </details>
                      ))}
                    </div>
                  )}

                  {/* 回答正文。&&短路:content还是空串(第一个token没到)时不渲染 */}
                  {m.content &&
                    // 生成中与完成后用两套渲染:
                    // done=false->默认渲染(朴素pre,扛得住每token重渲染)
                    // done=true->highlightComponents(代码块换shiki高亮)
                    (m.done ? (
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={highlightComponents}
                      >
                        {m.content}
                      </ReactMarkdown>
                    ) : (
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {m.content}
                      </ReactMarkdown>
                    ))}

                  {/* 闪烁光标:生成中且没出错时显示,提示"还在写" */}
                  {/* ▍:方块字符,配合index.css里的cursor动画做闪烁效果 */}
                  {!m.done && !m.error && <span className="cursor">▍</span>}

                  {/* 错误信息:有error才显示红字 */}
                  {m.error && <p className="error-text">{m.error}</p>}
                </>
              )}
            </div>
          </div>
        ))}

        { /* 滚动锚点:空div,ref挂上后靠scrollIntoView把它压进视野=滚到底 */}
        <div ref={bottomRef} />
      </div>

      { /* =====检索状态提示:statusText非空才显示===== */}
      {statusText && <p className="status-text">{statusText}</p>}

      { /* =====输入区===== */}
      <div className="input-area">
        { /* textarea:多行输入框(问题可能很长,单行input不够用) */}
        <textarea
          value={input} // 受控:显示值=input状态
          // onChange:每敲一个字触发,e.target.value是当前完整文本
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown} // Enter发送/Shift+Enter换行/中文输入法放行
          placeholder="输入问题,Enter发送,Shift+Enter换行"
          rows={2} // 默认显示2行高度
        />
        { /* disabled双条件:生成中禁发;输入框全空白也禁发 */}
        <button onClick={send} disabled={loading || !input.trim()}>
          {loading ? '生成中' : '发送'}
        </button>
      </div>

      { /* =====访客额度提示:siteCfg拿到才显示,拿不到就不打扰用户 ===== */}
      {siteCfg && (
        <p className="hint">
          {/* 三元:开了自带Key就说明"怎么提额",没开就只报当前额度 */}
          {siteCfg.byok_enabled
            ? `访客每日可提问 ${siteCfg.guest_daily_question_limit} 次;`
              + `在右上角配置自己的 LLM Key 后可提升到 ${siteCfg.byok_daily_question_limit} 次。`
            : `访客每日可提问 ${siteCfg.guest_daily_question_limit} 次。`}
        </p>
      )}
      </section>
    </section>
  )
}
