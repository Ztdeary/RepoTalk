/* 根组件兼简易路由,按「未登录->登录页/未选仓库->列表页/已选仓库->详情页」三种状态切换视图。*/

// 从react导入useState和useEffect 
// useState:React Hook,用于在函数组件里声明状态变量 
// useEffect:React Hook,用于处理副作用(如网络请求、订阅、手动操作DOM)
import {useState,useEffect} from 'react'

// 导入子组件
import Login from './components/Login'
import RepoList from './components/RepoList'
import RepoDetail from './components/RepoDetail'

// App:根组件,根据登录状态和选中的仓库渲染不同页面 
// 用简单的状态机做路由切换,不引入react-router,减少依赖
function App(){
  // =====状态1:token===== 
  // useState<string | null>(...):声明一个状态,类型是string或null
  // 初始值用函数形式:()=>localStorage.getItem('token') 
  // 为什么用函数?因为这个函数只在组件首次渲染时执行一次, 
  // 避免每次渲染都去读localStorage(虽然读localStorage很快,但这是好习惯)
  const [token,setToken]=useState<string | null>(()=>localStorage.getItem('token'))

  // =====状态2:选中的仓库ID===== 
  // number | null:有值表示在仓库详情页,null表示在仓库列表页
  const [selectedRepoId,setSelectedRepoId]=useState<number | null>(null)

  // =====副作用:token变化时同步到localStorage===== 
  // useEffect的第一个参数是副作用函数,第二个参数是依赖数组 
  // 依赖数组[token]:只有token变化时才重新执行副作用
  useEffect(()=>{
    if (token){
      // 有token:存到localStorage
      localStorage.setItem('token',token)
    }else {
      // token为null(登出):从localStorage删除 token
      localStorage.removeItem('token')
    }
  },[token])

  // =====渲染逻辑:根据状态决定显示哪个页面=====

  // 情况1:未登录->显示登录页
  if (!token){
    // <Login onLogin={setToken(token)=>setToken(token)}/>:渲染登录组件
    // onLogin属性:登录成功后调用setToken(token)把token存进状态
    return <Login onLogin={(t)=>setToken(t)}/>
  }

  // 情况2:已登录但没选仓库->显示仓库列表
  if (selectedRepoId===null){
    return (
      <RepoList
        // onSelectRepo:点击某个仓库时,把仓库ID存进状态,切换到详情页
        onSelectRepo={(id)=>setSelectedRepoId(id)}
        // onLogout:退出登录,把token设为null
        onLogout={()=>setToken(null)}
      />
    )
  }

  // 情况3:已登录且选了仓库->显示仓库详情
  return (
    <RepoDetail
      repoId={selectedRepoId}
      // onBack:返回列表,把选中的仓库ID设为null
      onBack={()=>setSelectedRepoId(null)}
      onLogout={()=>setToken(null)}
    />
  )
}

// export default:默认导出App组件,main.tsx里import App拿到的就是它
export default App
