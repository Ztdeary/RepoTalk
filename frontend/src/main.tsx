// 程序真正的启动点,用createRoot把<App/>渲染挂载到#root,并引入全局样式。
// 从 'react' 导入 StrictMode 组件
// StrictMode:React 严格模式,开发环境下会对组件做额外检查
// 比如:检测副作用、重复渲染、过时的 API 使用等
// 注意:StrictMode 只在开发环境生效,生产环境不会做这些检查
import { StrictMode } from 'react'

// 从 'react-dom/client' 导入 createRoot 函数
// createRoot:React 18 引入的新根 API,替代老的 ReactDOM.render
// 它支持并发渲染(Concurrent Rendering)
import { createRoot } from 'react-dom/client'

// 导入全局样式
// import './index.css':Vite 支持在 JS 里直接导入 CSS
// 构建时 Vite 会把它提取成单独的 CSS 文件
import './index.css'

// 导入根组件 App
// 注意:导入时写了 .tsx 扩展名,因为 tsconfig 里 allowImportingTsExtensions=true
import App from './App.tsx'

// document.getElementById('root'):获取 index.html 里 id="root" 的 div
// 感叹号 !:TypeScript 的非空断言,告诉 TS "这个元素一定存在,不是 null"
// createRoot(...):创建一个 React 根实例
// .render(...):把 React 组件渲染到这个根节点上
createRoot(document.getElementById('root')!).render(
  // <StrictMode>:严格模式包裹整个应用
  // <App />:根组件
  <StrictMode>
    <App />
  </StrictMode>,
)
