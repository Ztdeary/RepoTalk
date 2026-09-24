/* 仓库列表页组件,负责展示所有仓库及状态、提交新的仓库URL发起解析任务。*/

/* ============================================================
 * RepoList.tsx——仓库管理台
 * 在项目中的作用:
 * 登录成功后的第一个页面,干两件事:
 *   1) 顶部表单提交Git仓库URL->调后端POST/repos入队arq解析任务
 *      (这是整个RAG流程的前端起点)
 *   2) 下方列表展示所有仓库,状态翻译成中文+颜色显示
 *   点击某个仓库->调onSelectRepo通知App切换到详情页
 * ============================================================ */

// import:ES Module导入关键字,把别的模块的东西拿到当前文件用 
// {useState,useEffect}:解构导入——react包导出很多Hook,花括号表示"只要这两个" 
// useState:管理状态(值变化会触发界面重新渲染) 
// useEffect:处理副作用(挂载后自动拉列表这种"渲染之外的事") 
// from'react':写的是npm包名不是路径,Vite会去node_modules/react里找
import {useState,useEffect} from 'react'

// 从上一级目录(../ 表示上级)的api/client.ts导入: 
// listRepos:封装好的GET/repos调用,返回仓库数组 
// createRepo:封装好的POST/repos调用,创建解析任务 
// type Repo:数据类型定义(文件路径/状态/错误等字段的形状) 
// type 前缀:verbatimModuleSyntax配置要求——纯类型导入必须显式标记, 
// 编译时被擦除,不会打进最终JS
import {listRepos,createRepo,type Repo} from '../api/client'

// =====Props接口:本组件与父组件App之间的"合同"===== 
// interface:TS关键字,定义"对象的形状"(有哪些属性、各是什么类型) 
// 作用:对应App.tsx里<RepoList onSelectRepo={...} onLogout={...} /> 
// 如果App忘传某个属性,TS在App.tsx编译期就报错,不用等运行时崩
interface RepoListProps {
  // onSelectRepo:点击列表项的回调,把仓库id(number)"举"给父组件 
  // onSelectRepo:(repoId:number)=>void是函数类型:收一个number参数,不返回值
  onSelectRepo:(id:number)=>void
  // onLogout:退出登录回调,无参无返回——App把token设为null即回登录页
  onLogout:()=>void
}

// =====状态翻译表:后端英文状态->前端中文标签+颜色===== 
// const:声明常量,不能重新赋值(但对象内部属性可以改,这里我们也不改) 
// Record<string, {...}>:TS内置工具类型,"键是字符串、值是{label,color}对象"的字典 
// 在项目中的作用:用户看不懂pending/embedding,翻译成「排队中/向量化中」 
// 并配颜色——灰=没开始,橙=进行中,绿=成功,红=失败,一眼可辨
const STATUS_MAP:Record<string,{label:string;color:string}>={
  pending:    {label:'排队中',color:'#909399'}, // 灰:任务入队,worker未接
  cloning:    {label:'克隆中',color:'#e6a23c'}, // 橙:git clone进行中
  splitting:  {label:'切分中',color:'#e6a23c'}, // 橙:tree-sitter AST切分中
  embedding:  {label:'向量化中',color:'#e6a23c'}, // 橙:写pgvector中
  done:       {label:'已完成',color:'#67c23a'}, // 绿:可开始问答
  failed:     {label:'失败',color:'#f56c6c'}, // 红:解析出错  
}

// export default:默认导出,别的文件import RepoList from...拿到的就是它 
// function RepoList(...):普通函数声明,返回JSX就是React函数组件 
// {onSelectRepo,onLogout}:参数位置解构props对象,直接取出两个回调 
// RepoListProps:类型标注,props必须符合上面的合同形状
export default function RepoList({onSelectRepo,onLogout}:RepoListProps){

  // =====五个状态===== 
  // useState<Repo[]>([])的拆解: 
  // useState调用返回二元数组[当前值,修改函数] 
  // [repos,setRepos]:数组解构取出两者 
  // <Repo[]>:泛型,声明这个状态装的是"Repo对象数组" 
  // ([]):初始值——空数组(还没拉数据) 
  // 为什么不能直接repos=xxx?直接改React感知不到,不重新渲染; 
  // setRepos(xxx)才会触发重渲染,界面更新

  const [repos,setRepos]=useState<Repo[]>([]) // 仓库列表数据(后端拉回来的)
  const [loading,setLoading]=useState(true) // 列表加载中?true显示"加载中..."
  const [repoUrl,setRepoUrl]=useState('') // 新建表单:URL输入框的值
  const [collection,setCollection]=useState('') // 新建表单:collection名输入框的值
  const [creating,setCreating]=useState(false) // 提交中?禁用按钮防狂点重复提交
  const [createError,setCreateError]=useState('') // 创建失败的红字;空串=不显示


  // =====拉取仓库列表的函数===== 
  // 为什么封装成函数而不是直接写进useEffect?以后加"刷新按钮"能复用 
  // async:标记异步函数,内部可用await等Promise
  const fetchRepos=async()=>{
    // try/catch/finally三段式:try出错跳catch;finally不管成败都执行
    try{
      // await:挂起等后端响应(不阻塞页面,JS单线程靠事件循环调度) 
      // listRepos()内部走axios实例,拦截器自动塞JWT头
      const data=await listRepos()
      setRepos(data) // 数据进state->触发重渲染->列表显示出来
    }catch {
      // catch不带参数(ES2019语法):不关心错误内容 
      // 401由client.ts响应拦截器统一处理(清token+刷新回登录页)
      // 断网等其他错误静默——列表页不是关键路径,失败显示空列表即可,不弹错误打扰
    }finally{
      setLoading(false) // 不管成败,"加载中"必须结束,否则永远转圈
    }
  }
  
  // =====挂载时自动拉一次列表===== 
  // useEffect(函数,[]):第二个参数是依赖数组 
  // 空数组[]=只在组件首次渲染后执行一次,之后永不重跑 
  // 项目中的作用:用户登录进来第一眼就要看到自己的仓库,不能等手动操作
  useEffect(()=>{
    fetchRepos()
  },[])
  
  // =====提交"解析新仓库"表单===== 
  // e:React.FormEvent:浏览器传给事件处理函数的表单事件对象
  const handleCreate=async(e:React.FormEvent)=>{
    // preventDefault:阻止表单默认行为(整页刷新跳转) 
    // 单页应用必须AJAX悄悄发,页面一刷新React状态全丢
    e.preventDefault()
    // trim()去首尾空格;空串是falsy,!空串=true->拦截
    if (!repoUrl.trim()) return
  
    setCreating(true) // 按钮禁用+文字变"提交中..."
    setCreateError('') // 清掉上一次失败的红字,避免残留误导
    try{
      // collection.trim() || undefined的逐词逻辑: 
      // 用户填了collection->trim后是非空串(truthy)->||短路,用填的值 
      // 没填->trim后是空串(falsy)->取 || 右边的undefined 
      // 后端收到undefined会从URL最后一段自动提取仓库名; 
      // 且JSON.stringify会自动省略undefined字段,不会传成字符串"undefined"
      const repo=await createRepo(repoUrl.trim(),collection.trim() || undefined)
      setRepoUrl('') // 成功后清空两个输入框,方便连续建下一个
      setCollection('')
      // 创建成功直接跳进详情页看解析进度——省用户再手点一次 
      // onSelectRepo是App传的回调,执行后App的selectedRepoId变化->页面切换
      onSelectRepo(repo.id)
    }catch (err:any){
      // 双层可选链:err.response可能不存在(断网),?.防崩
      // FastAPI错误格式固定是{"detail":"..."},路径就是data.detail
      // || 兜底:取不到detail时显示通用文案
      setCreateError(err.response?.data?.detail || '创建失败')
    }finally{
      setCreating(false) // 恢复按钮,失败也能重试
    }
  }
  
  // =====JSX渲染===== 
  // return(...):多行JSX必须用括号包裹(防JS的ASI自动分号机制出错)
  return(
    // className而不是class:class是JS保留字,JSX改用className
    // 最终渲染成HTML的class属性,对应index.css里的样式
    <div className="page">
    
      {/* JSX里的注释必须包在花括号里:{/* ... *\/}
            顶栏:标题+退出按钮 */}
      <header className="header">
        <h1>RepoTalk</h1>
        {/* onClick={onLogout}:花括号是JSX的"JS表达式插槽",
              引号是字符串,花括号里才是变量/函数——这里把App传的回调直接绑给点击事件 */}
        <button className="logout-btn" onClick={onLogout}>退出登录</button>
      </header>
        
      <div className="content">
        
        {/* =====区域一:新建仓库表单===== */}
        <section className="create-section">
          <h2>解析新仓库</h2>
          {/* 用<form>而不是div+按钮:白捡回车提交和原生表单语义 */}
          <form onSubmit={handleCreate} className="create-form">
            {/* 受控组件:value绑死state,onChange把击键同步回state
                数据环:敲键盘->onChange->setState->重渲染->value写回输入框
                好处:React永远知道当前值,校验/清空/禁用都容易 */}
            <input
              type="text"
              placeholder="Git仓库HTTPS地址,如https://github.com/psf/requests"
              value={repoUrl}
              // e.target是触发事件的input元素,.value是它当前的完整文字
              // 作用:用户每敲一下键盘,就把输入框里的最新文字同步进repoUrl这个state,让React始终掌握表单的当前内容(受控组件的数据回流)。
              onChange={(e)=>setRepoUrl(e.target.value)}
              disabled={creating} // 提交中禁改,防用户改了导致提交内容不一致
            />
            <input
              type="text"
              placeholder="collection名(可选,默认取URL最后一段)"
              value={collection}
              onChange={(e)=>setCollection(e.target.value)}
              disabled={creating}
            />
            {/* disabled双保险:提交中禁+URL为空也禁(空串trim后是falsy) */}
            <button type="submit" disabled={creating || !repoUrl.trim()}>
              {/* 三元表达式:状态驱动文字,用户一眼看出系统在干活 */}
              {creating?'提交中...':'开始解析'}
            </button>
          </form>
          {/* &&短路渲染:createError是空串(falsy)时整个表达式短路,什么都不渲染;
              有内容(truthy)时渲染右边红字div */}
          {createError && <div className="error">{createError}</div>}
        </section>
          
        {/* =====区域二:仓库列表===== */}
        <section className="repo-list-section">
          <h2>我的仓库</h2>
          {/* 三态互斥渲染:加载中/空列表/有数据,同一时刻只显示一种
              嵌套三元:loading?A:(repos空?B:C) */}
          {loading?(
            <p>加载中...</p>
          ):repos.length===0?(
            <p>暂无仓库,先在上面提交一个吧</p>
          ):(
            <ul className="repo-list">
              {/* map:遍历数组,每个元素返回一段JSX,React拼成列表
                  key={repo.id}:React diff算法靠key识别"哪项变了/删了/加了",
                  用数据库自增主键最稳定(千万别用数组下标,顺序一变就乱) */}
              {repos.map((repo)=>{
                // 查翻译表;查不到(后端新增状态前端没跟上)用原值兜底,不崩
                // STATUS_MAP[repo.status]:字符串当键查字典
                // || {...}:查不到返回undefined(falsy),用右边默认对象
                const st=STATUS_MAP[repo.status] || {label:repo.status,color:'#909399'}
                return (
                  <li
                    key={repo.id}
                    className="repo-item"
                    // 箭头函数包一层:因为onClick要的是"函数",
                    // 直接写onSelectRepo(repo.id)会立即执行而不是等点击
                    onClick={()=>onSelectRepo(repo.id)}
                  >
                    <div className="repo-info">
                      {/* JSX插值:{表达式}把JS值渲染进HTML */}
                      <span className="repo-name">{repo.collection}</span>
                      <span className="repo-url">{repo.repo_url}</span>
                    </div>
                    {/* style内联样式:值是JS对象,属性名驼峰(color不用变),
                        颜色随状态动态变化——这就是翻译表里color字段的用处 */}
                    <span className="repo-status" style={{color:st.color}}>
                      {st.label}
                    </span>
                  </li>
                )
              })}
            </ul>
          )}
        </section>
      </div>
    </div>
  )
}
