// 配置Vite开发服务器,用/api代理把前端请求转发到后端8000端口,从而绕开浏览器跨域限制。

// 从@vitejs/plugin-react导入默认导出react 
// react是一个函数,调用后返回Vite插件对象,负责把JSX/TSX编译成JS
import react from '@vitejs/plugin-react'

// 从vite导入defineConfig函数 
// defineConfig的作用:给配置对象加上TypeScript类型提示,写配置时有自动补全
import {defineConfig} from 'vite'

// export default:ES Module的默认导出,其他文件import这个文件时拿到的就是这个对象\
export default defineConfig({
  // plugins:Vite插件数组
  // react():调用react函数得到React插件实例
  plugins:[react()],

  // server:开发服务器相关配置
  server:{
    // proxy:代理配置,把前端的某些请求转发到后端
    proxy:{
      // 键/api:所有以/api开头的请求都走这个代理规则
      '/api':{
        // target:转发目标地址,即后端FastAPI服务地址
        target:'http://localhost:8000',
        // changeOrigin:true:把请求头里的Host改成target的地址 
        // 作用:有些后端会检查Host头,不修改会被拒绝
        changeOrigin:true,
        // rewrite:重写路径的函数,参数path是原始请求路径 
        // path.replace(/^\/api/,''):把路径开头的/api删掉 
        // 例:前端请求/api/repos->转发到后端变成/repos
        rewrite:(path)=>path.replace(/^\/api/,''),
      }
    }
  }
})
