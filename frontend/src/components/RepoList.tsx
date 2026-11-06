/* 仓库列表页:访客与管理员共用。访客可浏览/进入问答;提交新仓库需自带 LLM Key。*/

/* ============================================================
 * RepoList.tsx——仓库管理台(访客模式改造版)
 * 在项目中的作用:
 *   1) 顶部表单提交 Git 仓库 URL -> 后端 POST /repos 入队解析任务
 *      访客模式下:请求头会自动带上他自己的 LLM Key(见 api/session.ts)
 *      没有 Key 时后端会返回 403,前端把原因显示在表单下方
 *   2) 下方列表展示所有仓库,状态翻译成中文+颜色
 *   3) 右上角「自带 Key」入口:访客可配置自己的模型,费用记在自己账上
 * ============================================================ */

// useState/useEffect:状态与副作用
import {useState,useEffect} from 'react'

// listRepos:GET /repos
// createRepo:POST /repos
// getSiteConfig:GET /demo/config(判断是否显示"需配置 Key"的提示)
// Repo / SiteConfig:类型
import {
  listRepos,
  createRepo,
  deleteRepo,
  getSiteConfig,
  type Repo,
  type SiteConfig,
} from '../api/client'

// 自带 Key 的设置入口组件(按钮+弹窗)
import ApiSettings from './ApiSettings'
// hasCred:判断当前标签页是否已配置 Key(配置变化后要重新读一次)
import {hasCred} from '../api/session'
// 管理员数据看板:今日/近7天/近30天的访问与问答统计
// 只在登录状态下渲染(见下面 content 顶部),访客看不到
import StatsDashboard from './StatsDashboard'

// =====Props接口=====
interface RepoListProps{
  // onSelectRepo:点击列表项时把仓库id交给 App
  onSelectRepo:(id:number)=>void
  // onLogin:访客点"管理员登录"时调用;已登录时 App 传 undefined
  onLogin?:()=>void
  // onLogout:管理员点"退出登录"时调用;访客时 App 传 undefined
  onLogout?:()=>void
  // 两者都是可选:组件不关心"到底登录没登录",
  // 只看"哪一个回调被传进来"来决定右上角显示哪个按钮
}

// =====状态翻译表=====
// Record<string,{label;color}>:键是后端状态字符串,值是中文标签+颜色
const STATUS_MAP:Record<string,{label:string;color:string}>={
  pending:    {label:'排队中',color:'#909399'}, // 灰:已入队,worker未接
  cloning:    {label:'克隆中',color:'#e6a23c'}, // 橙:git clone 进行中
  splitting:  {label:'切分中',color:'#e6a23c'}, // 橙:AST 切分中
  embedding:  {label:'向量化中',color:'#e6a23c'}, // 橙:写 pgvector
  done:       {label:'已完成',color:'#67c23a'}, // 绿:可开始问答
  failed:     {label:'失败',color:'#f56c6c'}, // 红:解析出错
}

export default function RepoList({onSelectRepo,onLogin,onLogout}:RepoListProps){

  // =====列表状态=====
  const [repos,setRepos]=useState<Repo[]>([])      // 仓库数组
  const [loading,setLoading]=useState(true)        // 首次加载中
  const [repoUrl,setRepoUrl]=useState('')          // 表单:URL
  const [collection,setCollection]=useState('')    // 表单:collection(可选)
  const [creating,setCreating]=useState(false)     // 提交中(防重复点击)
  const [createError,setCreateError]=useState('')  // 提交失败的红字

  // =====访客模式状态=====
  // cfg:站点配置。null=还没拿到,拿不到时按"没有特殊限制"处理(不让表单被无辜禁用)
  const [cfg,setCfg]=useState<SiteConfig|null>(null)
  // hasKey:当前是否已配置自带 Key。
  // 为什么要单独放进 state 而不是每次渲染都调 hasCred():
  // sessionStorage 不是响应式数据,ApiSettings 保存后必须靠 setState 触发重渲染,
  // 提示文字才会立刻消失
  const [hasKey,setHasKey]=useState(hasCred())

  // =====删除仓库状态(仅管理员)=====
  // confirmId:正在等待二次确认的仓库 id。
  // 为什么要点两次:删除会连同向量数据一起清掉、不可恢复,
  // 误点一下的代价太大;null 表示当前没有待确认的项
  const [confirmId,setConfirmId]=useState<number|null>(null)
  // deletingId:正在请求删除的仓库 id,用来禁用按钮防止连点发多次请求
  const [deletingId,setDeletingId]=useState<number|null>(null)
  // deleteError:删除失败的红字(比如 409 解析中、404 已不存在)
  const [deleteError,setDeleteError]=useState('')

  // =====拉列表=====
  const fetchRepos=async()=>{
    try{
      // 访客也能拉:后端 GET /repos 已放开(只读演示的前提)
      const data=await listRepos()
      setRepos(data)
    }catch {
      // 失败静默:列表页不是关键路径,不清空已有数据更好
    }finally{
      // 不管成败都要结束"加载中",否则界面永远转圈
      setLoading(false)
    }
  }

  // =====挂载:拉列表 + 拉配置=====
  useEffect(()=>{
    fetchRepos()
    // 配置拉取失败只影响"提示文案",不影响主流程,所以 catch 里只置 null
    getSiteConfig().then(setCfg).catch(()=>setCfg(null))
  },[]) // 空依赖:只在首次渲染后跑一次

  // =====提交新仓库=====
  const handleCreate=async(e:React.FormEvent)=>{
    // 阻止表单默认的整页刷新
    e.preventDefault()
    if (!repoUrl.trim()) return

    setCreating(true)
    setCreateError('')
    try{
      // collection 为空就传 undefined:
      // JSON.stringify 会省略 undefined 字段,后端才会自动从 URL 取名
      const repo=await createRepo(repoUrl.trim(),collection.trim() || undefined)
      setRepoUrl('')
      setCollection('')
      // 创建成功直接进详情页看解析进度
      onSelectRepo(repo.id)
    }catch (err:any){
      // FastAPI 的错误体是 {"detail":"..."};访客没配 Key 时后端会返回中文原因,
      // 直接展示,用户才知道下一步该点右上角
      setCreateError(err.response?.data?.detail || '创建失败')
    }finally{
      setCreating(false)
    }
  }

  // =====删除仓库(仅管理员)=====
  const handleDelete=async(repoId:number)=>{
    setDeletingId(repoId) // 标记"这个仓库正在删",按钮变灰防连点
    setDeleteError('')    // 先清掉上一次的错误
    try{
      await deleteRepo(repoId)
      // 本地直接摘掉这一行,而不是重新拉整张表:
      // 后端已经确认删除成功,本地同步移除最即时,也不会因为网络抖动拉不到新列表
      setRepos(prev=>prev.filter(r=>r.id!==repoId))
      setConfirmId(null)
    }catch(err:any){
      // 404(已被别人删掉)/401(token过期)都带中文 detail
      setDeleteError(err.response?.data?.detail || '删除失败')
      setConfirmId(null)
    }finally{
      setDeletingId(null)
    }
  }

  // 是否需要提示"先配置Key"?
  // 三个条件同时成立:开关打开 && 当前没Key && 确实拿到了配置。
  // 用 && 短路而不是嵌套 if,布尔表达式直接给 JSX 用
  const needKeyHint = !!cfg?.byok_enabled && !hasKey

  return(
    <div className="page">

      {/* =====顶栏:标题 + 右侧操作区===== */}
      <header className="header">
        <h1>RepoTalk</h1>

        {/* header-actions:把多个按钮包成一组的容器,
            这样 .header 的 space-between 仍然只有"左标题 / 右动作区"两块,布局不乱 */}
        <div className="header-actions">
          {/* 自带 Key 入口:内部自己判断 byok_enabled,关闭时它返回 null 什么都不显示 */}
          <ApiSettings
            // 保存/清除后重新读一次 hasCred,让下面的提示立刻更新
            onChanged={()=>setHasKey(hasCred())}
          />

          {/* 有 onLogout = 当前是管理员,显示退出;否则显示登录入口 */}
          {onLogout ? (
            <button className="logout-btn" onClick={onLogout}>退出登录</button>
          ) : onLogin ? (
            <button className="logout-btn" onClick={onLogin}>管理员登录</button>
          ) : null}
          {/* ? : null 三元链:两个回调都没传时明确返回 null,不渲染空按钮 */}
        </div>
      </header>

      <div className="content">

        {/* =====区域零:管理员数据看板=====
            onLogout 只在管理员登录时由 App 传入,
            所以这个条件等价于"当前是管理员",访客不会看到任何统计数据 */}
        {onLogout && <StatsDashboard/>}

        {/* =====区域一:提交新仓库===== */}
        <section className="create-section">
          <h2>解析新仓库</h2>

          {/* 访客没配 Key 时的提示:提前告知,比提交后再报 403 体验好 */}
          {needKeyHint && (
            <p className="hint">
              提交新仓库需要先在右上角「自带 Key」里配置你自己的 LLM Key;
              只浏览和提问不需要配置。
            </p>
          )}

          <form onSubmit={handleCreate} className="create-form">
            <input
              type="text"
              placeholder="Git仓库HTTPS地址,如https://github.com/psf/requests"
              value={repoUrl}
              // e.target.value:输入框此刻的完整文字,同步进 state
              onChange={(e)=>setRepoUrl(e.target.value)}
              disabled={creating}
            />
            <input
              type="text"
              placeholder="collection名(可选,默认取URL最后一段)"
              value={collection}
              onChange={(e)=>setCollection(e.target.value)}
              disabled={creating}
            />
            {/* 禁用条件:提交中 或 URL 为空。
                这里不因"没 Key"而禁用:
                配置可能刚被保存但 state 还没刷新,而且后端报错信息更准确 */}
            <button type="submit" disabled={creating || !repoUrl.trim()}>
              {creating?'提交中...':'开始解析'}
            </button>
          </form>

          {/* && 短路:空串不渲染,有内容才显示红字 */}
          {createError && <div className="error">{createError}</div>}
        </section>

        {/* =====区域二:仓库列表===== */}
        <section className="repo-list-section">
          {/* 访客看到的是全站仓库,所以标题从"我的仓库"改成中性的"仓库列表" */}
          <h2>仓库列表</h2>

          {/* 三态互斥:加载中 / 空 / 有数据 */}
          {loading?(
            <p>加载中...</p>
          ):repos.length===0?(
            <p>暂无仓库,先在上面提交一个吧</p>
          ):(
            <ul className="repo-list">
              {repos.map((repo)=>{
                // 查翻译表;查不到就用原始状态值兜底,保证以后后端加状态也不崩
                const st=STATUS_MAP[repo.status] || {label:repo.status,color:'#909399'}
                return (
                  <li
                    key={repo.id} // React diff 靠稳定主键识别列表项
                    className="repo-item"
                    // 箭头函数包一层:onClick 需要"函数",直接调用会在渲染时立刻执行
                    onClick={()=>onSelectRepo(repo.id)}
                  >
                    <div className="repo-info">
                      <span className="repo-name">{repo.collection}</span>
                      <span className="repo-url">{repo.repo_url}</span>
                    </div>

                    {/* 右侧:状态 + 管理员删除按钮。
                        onLogout 只在管理员登录时有值,访客看不到任何删除入口 */}
                    <div className="repo-item-right">
                      <span className="repo-status" style={{color:st.color}}>
                        {st.label}
                      </span>

                      {onLogout && (
                        // stopPropagation:按钮在整行 onClick 里面,
                        // 不拦住的话点删除会同时触发"进入仓库详情"
                        <div className="repo-actions" onClick={(e)=>e.stopPropagation()}>
                          {confirmId===repo.id ? (
                            <>
                              <button
                                className="repo-del-confirm"
                                disabled={deletingId===repo.id}
                                onClick={()=>handleDelete(repo.id)}
                              >
                                {deletingId===repo.id?'删除中...':'确认删除'}
                              </button>
                              <button
                                className="repo-del-cancel"
                                disabled={deletingId===repo.id}
                                onClick={()=>setConfirmId(null)}
                              >
                                取消
                                 {/* 取消时只收起确认条,不发任何请求 */}
                              </button>
                            </>
                          ) : (
                            <button
                              className="repo-del-btn"
                              // 任何状态都可删(2026-11 起):解析中的仓库也允许清理,
                              // 后端会通知 worker 停止,不会再往 collection 写数据
                              title="删除该仓库及其向量数据"
                              onClick={()=>{
                                setConfirmId(repo.id) // 第一次点击只进入"待确认"状态
                                setDeleteError('')
                              }}
                            >
                              删除
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  </li>
                )
              })}
            </ul>
          )}

          {/* 删除失败提示:放在列表下方,不遮挡操作区 */}
          {deleteError && <div className="error">{deleteError}</div>}
        </section>
      </div>
    </div>
  )
}
