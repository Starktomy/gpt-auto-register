import { createApp } from 'vue'
import { createPinia } from 'pinia'

// Element Plus 样式按需引入（vite 插件会给模板里用到的组件自动加样式）。
// 程序式调用的 ElMessage / ElMessageBox / v-loading 不经过模板解析，必须手动引入。
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/loading/style/css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles/theme.css'
import 'nprogress/nprogress.css'

// 只注册实际用到的图标（动态 <component :is="name"> 需要全局注册）
import {
  Platform, Fold, Expand, Moon, Sunny, User, ArrowDown,
  Odometer, Upload, VideoPlay, MagicStick, Connection, Files,
  CircleCheck, Document, Message, Iphone, Share,
  Loading, Select, CircleClose, Refresh, CopyDocument,
  Bell, Close, Download, Delete, Link, CreditCard,
} from '@element-plus/icons-vue'

import App from './App.vue'
import router from './router'

const ICONS = {
  Platform, Fold, Expand, Moon, Sunny, User, ArrowDown,
  Odometer, Upload, VideoPlay, MagicStick, Connection, Files,
  CircleCheck, Document, Message, Iphone, Share,
  Loading, Select, CircleClose, Refresh, CopyDocument,
  Bell, Close, Download, Delete, Link, CreditCard,
}

const app = createApp(App)
for (const [name, comp] of Object.entries(ICONS)) app.component(name, comp)

app.use(createPinia())
app.use(router)
app.mount('#app')
