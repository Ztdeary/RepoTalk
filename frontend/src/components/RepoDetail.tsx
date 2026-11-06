/* 仓库详情页组件,先查一次状态兜底再订阅/repos/{id}/events SSE实时展示解析进度,完成后渲染聊天面板。*/

/* ============================================================
 * RepoDetail.tsx——仓库详情页:解析进度监控屏+问答入口
 * 在项目中的作用:
 * 用户从列表页点某个仓库进来后,本组件负责:
 * 1) 实时显示 arq worker 的解析进度(克隆中->切分中->向量化中->完成)
 * 2) 解析完成后挂载 ChatPanel,开放问答
 * 进度采用「数据库查询兜底+SSE订阅实时更新」双保险,
 * 防止用户"来晚了"(解析已结束)时永远停在"排队中"
 * ============================================================ */

import { useState, useEffect } from 'react'
// 从上一级目录的api/client.ts导入:
// getRepo:GET/repos/{id},一次性查询仓库当前状态(兜底用) 
// sseRequest:手写的SSE客户端,订阅GET/repos/{id}/events进度推送流
import { getRepo, sseRequest, type Repo } from '../api/client'
// 从同级目录导入聊天面板组件 
// 在项目中的作用:本组件只在解析完成(stage==='done')后才挂载它
import ChatPanel from './ChatPanel'
// 自带 Key 入口:详情页也要能配置,否则访客从列表点进来后就找不到设置了
import ApiSettings from './ApiSettings'

// =====Props接口:本组件与父组件App之间的"合同"===== 
// 作用:对应App.tsx里<RepoDetail repoId={...} onBack={...} onLogout={...}/> 
interface RepoDetailProps {
  // repoId:number——App传的当前仓库id 
  // 用途:SSE订阅URL和数据库查询都靠它拼路径(/repos/{id}/events)
  repoId: number
  // onBack:()=>void——返回列表回调 
  // 作用:App把selectedRepoId设回null,App重新渲染->切回RepoList
  onBack: () => void
  // onLogout?:()=>void——退出登录回调(可选)。
  // 访客模式下 App 不传它,此时顶栏显示"管理员登录"而不是"退出登录"
  onLogout?: () => void
  // onLogin?:()=>void——访客点"管理员登录"时调用(可选)
  onLogin?: () => void
}

// =====解析阶段中文文案翻译表===== 
// Record<string,string>:键是任意字符串,值是字符串的字典 
// 在项目中的作用:用户看不懂pending/embedding,翻译成「排队中/向量化中」 
// done/failed带emoji,一眼区分成功失败
const STAGE_LABEL: Record<string, string> = {
  pending: '排队中...',
  cloning: '正在克隆仓库...',
  splitting: '正在切分仓库...',
  embedding: '正在向量化...',
  done: '解析完成✅',
  failed: '解析失败❌'
}

export default function RepoDetail({ repoId, onBack, onLogout, onLogin }: RepoDetailProps) {

  // =====三个状态===== // 

  // repo:仓库完整信息。初始null=数据库还没查回来 
  // 作用:标题栏显示repo.collection,以及SSE消息回来时同步更新status/error
  const [repo, setRepo] = useState<Repo | null>(null)

  // stage:当前解析阶段字符串 
  // 作用:1) 驱动进度文案显示 2) 决定是否挂载聊天面板(stage==='done'才挂载) 
  // 初始'pending':假设还没开始,等数据库/SSE修正
  const [stage, setStage] = useState<string>('pending')

  // error:解析失败或SSE连接中断时的错误信息 
  // 空串=没错误,不显示;有内容就红字号在进度条旁边
  const [error, setError] = useState<string>('')

  // =====核心副作用:兜底查询+SSE订阅===== 
  // useEffect(函数,[repoId]):依赖数组含repoId 
  // repoId变化时(从仓库A切到仓库B),旧订阅先清理,整个流程按新仓库重走一遍
  useEffect(() => {
    // abortSSE:存sseRequest返回的中断函数 
    // let而不用const:声明时还没值,订阅成功后才赋值 
    // (()=>void)|null:类型是"无参无返回函数或null" 
    // 作用:组件卸载时调它掐断fetch流,防止连接泄漏
    let abortSSE: (() => void) | null = null

    // mounted标志位:true=组件还在屏幕上,false=已被卸载 
    // 作用:异步回调(.then/onMessage)执行时,组件可能已被用户切走, 
    // 此时再setState会触发React警告且毫无意义,用这个布尔值挡一道
    let mounted = true

    // -----第一道保险:查数据库兜底----- 
    // 为什么需要:用户可能解析完成后才打开这个页面
    // 此时SSE频道早已没消息,只靠订阅会永远停在"排队中" 
    // 先查一次数据库当前状态,来晚了也能立即看到正确终态
    getRepo(repoId)
      // .then(data):Promise成功路径,data是后端返回的Repo对象
      .then((data) => {
        if (!mounted) return // 组件已卸载,丢弃这次结果
        setRepo(data) // 存完整信息(标题栏用)
        setStage(data.status) // 同步当前阶段
        if (data.error) setError(data.error) // 有错误就显示
      })
      // .catch:Promise失败路径(网络断/后端崩)
      .catch(() => {
        if (mounted) setError('获取仓库信息失败')
      })

    // -----第二道保险:订阅SSE实时进度----- 
    // 数据链路: 
    // arq worker推进一步->_emit发Redis频道repo:{id} 
    // ->后端GET/repos/{id}/events转发给前端 
    // ->sseRequest收到->onMessage回调执行 
    // sseRequest返回值就是abort函数,存进abortSSE变量
    abortSSE = sseRequest(`/repos/${repoId}/events`, {

      // onMessage:每收到一条SSE消息就调一次 
      // data参数:后端发的JSON字符串, 
      // 形如{"repo_id":5,"stage":"splitting","error":null}
      onMessage: (data) => {
        // JSON.parse:JSON字符串->JS对象
        const msg = JSON.parse(data)
        // 更新进度文案(克隆中->切分中->向量化中...)
        setStage(msg.stage)
        // 有错误信息就显示
        if (msg.error) setError(msg.error)
        // 函数式更新:用更新后的旧值推出更新的新值
        // 为什么不用setRepo({...repo,...})? 
        // 因为回调里的repo是闭包捕获的旧值,异步场景可能已过期
        // prev才是React保证的最新state,异步回调必须用函数式更新 
        // ...prev:展开旧对象所有字段;后面的status/error覆盖同名字段
        setRepo((prev) =>
          prev ? { ...prev, status: msg.stage, error: msg.error } : prev,
        )
      },

      // onError:SSE连接出错时调(如断网) 
      // err.message:Error对象的错误信息
      onError: (err) => {
        if (mounted) setError(`连接中断:${err.message}`)
      },
    })

    // -----清理函数:useEffect的返回值----- 
    // 执行时机:1) 组件卸载时 2) 依赖数组变化(仓库切换)时,先清旧的再建新的 
    // 不做会怎样:切走页面后SSE连接还挂着,后台一直收消息, 
    // 切几个仓库泄漏几个连接,服务器和浏览器资源白吃
    return () => {
      mounted = false // 先落闸:之后的异步回调全部被挡
      abortSSE?.() // 再掐流:?.是可选链——订阅还没建立时abortSSE是null,不崩
    }
  }, [repoId]) // 依赖:repoId变就重跑

  // =====JSX 渲染===== 
  // return(...):多行JSX必须用括号包裹,防ASI自动分号机制出错
  return (
    <div className="page">
      {/* JSX注释必须包在花括号里:{/* ... *\/}
        顶栏:返回按钮+仓库名+退出按钮 */}
      <header className="header">
        {/* onBack:App传的回调,点击后selectedRepoId变null->切回列表页 */}
        <button className="back-btn" onClick={onBack}>返回列表</button>
        {/* repo?.collection:可选链——repo还是null(数据库没查回来)时不崩,
          返回undefined;|| 兜底用「仓库#id」,标题栏永不空白 */}
        <h1>{repo?.collection || `仓库${repoId}`}</h1>

        {/* 右侧动作区:自带Key入口 + 登录/退出。
            包成一个 div 后 .header 仍然只有三个子元素(返回按钮/标题/动作区),
            space-between 布局不会被撑乱 */}
        <div className="header-actions">
          <ApiSettings />
          {/* onLogout 存在=管理员;否则有 onLogin 就显示登录入口 */}
          {onLogout ? (
            <button className="logout-btn" onClick={onLogout}>退出登录</button>
          ) : onLogin ? (
            <button className="logout-btn" onClick={onLogin}>管理员登录</button>
          ) : null}
        </div>
      </header>

      <div className="content">

        {/* =====进度面板===== */}
        <section className="progress-section">
          <div className="progress-info">
            {/* STAGE_LABEL[stage] || stage:
            翻译表查不到(后端新增状态前端没跟上)就显示原始英文值,向后兼容 */}
            <span className="stage-label">{STAGE_LABEL[stage] || stage}</span>
            {/* && 短路:error是空串(falsy)时不渲染,有内容才显示红字 */}
            {error && <span className="error-text">{error}</span>}
          </div>
          {/* stage!=='done'时显示提示语;
          完成后隐藏——聊天面板出现已经说明一切,不需要再提示 */}
          {stage !== 'done' && (<p className="hint">仓库解析完成后即可开始问答</p>
          )}
        </section>

        {/* =====聊天面板:只有解析完成才挂载=====
      为什么未完成不给聊?
      向量库里还没数据,问了也检索不到相关代码块,白烧token。
      && 短路:
      stage不是done时整个表达式为false,React不渲染任何内容;
      变done的瞬间挂载 ChatPanel,它的内部状态(messages 等)从全新开始 */}
        {stage === 'done' && <ChatPanel repoId={repoId} repoUrl={repo?.repo_url} />}
        {/* 这里不写 ?? '':repo可能还没查回来,空值要如实传给ChatPanel,
        由它在渲染时判断"不显示跳转链接",而不是拼出一个空地址的死链 */}
      </div>
    </div>
  )
}
