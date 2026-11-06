/* 根组件兼简易路由:不再"未登录必登录",而是访客直接进只读演示,管理员按需登录。*/

// useState:声明会触发重渲染的状态;useEffect:处理副作用(上报访问、同步token)
import {useState,useEffect} from 'react'

// 三个页面组件
import Login from './components/Login'
import RepoList from './components/RepoList'
import RepoDetail from './components/RepoDetail'
// 访问上报:页面挂载时调一次,后端统计 PV/UV
import {recordVisit} from './api/client'

function App(){
  // =====状态1:管理员token=====
  // 初始值用函数形式,只在首次渲染读一次 localStorage
  // 有token = 管理员身份(免配额);null = 普通访客
  const [token,setToken]=useState<string | null>(()=>localStorage.getItem('token'))

  // =====状态2:当前选中的仓库=====
  // null = 停在列表页;有数字 = 进入该仓库的详情/问答页
  const [selectedRepoId,setSelectedRepoId]=useState<number | null>(null)

  // =====状态3:是否显示登录页=====
  // 访客模式的改动点:登录不再是"进门强制",而是"主动选择"。
  // 平时 false(直接看演示),点右上角"管理员登录"才置 true
  const [showLogin,setShowLogin]=useState(false)

  // =====副作用1:token 与 localStorage 双向同步=====
  useEffect(()=>{
    if (token){
      // 有token:持久化,刷新页面后仍是管理员
      localStorage.setItem('token',token)
    }else{
      // 登出:删掉,刷新后回到访客模式
      localStorage.removeItem('token')
    }
  },[token]) // 依赖只有 token:其他状态变化不触发这个副作用

  // =====副作用2:上报一次访问(同一标签页只报一次)=====
  // 依赖数组 []:只在 App 首次挂载时执行一次。
  // 不这样做的后果:每次 token/选仓库变化都上报,PV 会被严重注水
  useEffect(()=>{
    // sessionStorage 里的标记 = "这个标签页本次会话已经报过到了"。
    // 为什么用 sessionStorage 而不是 localStorage:
    //   sessionStorage 的生命周期止于标签页关闭,刷新页面它还在 ->
    //   用户按 F5 不会重复上报;但新开一个标签页算一次新访问,符合直觉。
    // 为什么还要后端去重:前端只能挡住"自己这个浏览器",
    //   脚本直接 POST /demo/visit、清缓存、换浏览器都能绕过,
    //   所以后端在 Redis 里还有一个 30 分钟的窗口去重(见 stats.py)。
    if (sessionStorage.getItem('visit_reported')) return
    // catch 里什么都不做:统计失败绝不能影响用户正常使用,
    // 也不该弹错误提示去打扰访客
    recordVisit()
      // 只有真正上报成功才写标记;失败的话下次刷新会重试
      .then(()=>sessionStorage.setItem('visit_reported','1'))
      .catch(()=>{})
  },[])

  // =====情况1:用户主动点了"管理员登录" =====
  if (showLogin){
    return (
      <Login
        onLogin={(t)=>{
          setToken(t)          // 记下token -> 变成管理员身份
          setShowLogin(false)  // 关掉登录页,回到列表
        }}
        // onCancel:取消登录,回到访客演示。
        // 没有这个回调,访客点了登录就"出不去"了,只能关网页
        onCancel={()=>setShowLogin(false)}
      />
    )
  }

  // =====情况2:没选仓库 -> 列表页(访客和管理员共用)=====
  if (selectedRepoId===null){
    return (
      <RepoList
        onSelectRepo={(id)=>setSelectedRepoId(id)}
        // onLogin:只在没有token时传。RepoList 据此决定显示
        // "管理员登录"还是"退出登录"
        onLogin={token ? undefined : ()=>setShowLogin(true)}
        onLogout={token ? ()=>{
          // 退出登录时顺手回到列表页:管理员可能正停在某个仓库详情,
          // 不重置 selectedRepoId 会继续停留在详情页,但身份已变
          setSelectedRepoId(null)
          setToken(null)
        } : undefined}
      />
    )
  }

  // =====情况3:选了仓库 -> 详情/问答页 =====
  return (
    <RepoDetail
      repoId={selectedRepoId}
      onBack={()=>setSelectedRepoId(null)}
      // 详情页同样保留登录/退出入口,避免访客进到详情后"找不到入口"
      onLogin={token ? undefined : ()=>setShowLogin(true)}
      onLogout={token ? ()=>{
        setSelectedRepoId(null)
        setToken(null)
      } : undefined}
    />
  )
}

// 默认导出:main.tsx 里 import App 拿到的就是它
export default App
