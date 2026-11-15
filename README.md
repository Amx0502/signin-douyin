# signin-douyin

抖音私信「续火」自动化管理系统（独立版）—— 多账号扫码登录、定时续火任务、Cookie 自动续期、企业微信通知，开箱即用。

> 从多平台签到系统 [SignIn](https://github.com/) 中提取的抖音模块独立运行版：后端 FastAPI + SQLite，前端 Vue3 + Element Plus + Vite。

## 功能特性

- **扫码登录**：Playwright 无头浏览器扫码，自动解析主站身份（passport 优先，失败回退浏览器）
- **续火任务**：按目标用户（支持别名）定时发送私信保持火花，支持自定义消息模板、随机延时
- **Cookie 自动续期**：会话临近过期时自动刷新，失效账号自动标记
- **账号管理**：多账号绑定、状态监控（登出/风控/正常）、运行日志实时查看
- **企业微信通知**：任务执行结果推送（可选配置）
- **内置鉴权**：独立用户体系，首次启动自动创建管理员
- **零配置数据库**：SQLite 自动建库建表，无需迁移

## 目录结构

```
signin-douyin/
├── backend/
│   ├── app/
│   │   ├── main.py               # FastAPI 入口（登录 + 业务路由 + 静态托管）
│   │   ├── auth.py               # 登录 / token 签发校验
│   │   ├── auth_models.py        # 用户表
│   │   ├── douyin_database.py    # SQLite 数据库 + 幂等补列
│   │   ├── douyin_router.py      # 业务 API 路由
│   │   ├── douyin_service.py     # 业务逻辑层
│   │   ├── douyin_login.py       # 扫码登录流程
│   │   ├── douyin_client.py      # 抖音接口客户端
│   │   ├── douyin_identity.py    # 身份解析
│   │   ├── douyin_cookie_refresh.py  # Cookie 续期
│   │   ├── douyin_browser_sender.py  # 浏览器发消息
│   │   ├── douyin_scheduler.py   # 任务调度
│   │   └── ...
│   ├── run.py                    # uvicorn 启动器
│   ├── requirements.txt
│   └── tests/                    # 176 条单测
└── frontend/
    ├── src/
    │   ├── views/Login.vue       # 登录页
    │   ├── views/Douyin.vue      # 主控制台
    │   ├── api/                  # 接口封装
    │   └── components/douyin/    # 账号/任务/日志等组件
    └── vite.config.js
```

## 快速开始

### 后端

```bash
cd backend
pip install -r requirements.txt

# 首次启动（可选：指定初始管理员密码，不指定则自动生成并打印一次）
set ADMIN_PASSWORD=admin123    # Windows
# export ADMIN_PASSWORD=admin123  # Linux/macOS

python run.py                  # 默认 http://0.0.0.0:8090
```

首次启动自动创建 SQLite 数据库（`backend/data/douyin.db`）与管理员账号。

### 前端

```bash
cd frontend
npm install
npm run dev                     # 开发模式，默认 http://localhost:5173
npm run build                   # 生产构建，产物在 frontend/dist
```

开发模式下 API 代理已配置指向 `http://127.0.0.1:8090`。

生产部署：`npm run build` 后将 `frontend/dist` 托管给任意静态服务器（或直接放到后端静态目录，`main.py` 已内置静态托管）。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8090` | 后端监听端口 |
| `ADMIN_USERNAME` | `admin` | 初始管理员用户名 |
| `ADMIN_PASSWORD` | 随机生成 | 初始管理员密码（仅建库时生效，打印一次） |
| `DOUYIN_DB_URL` | `sqlite:///data/douyin.db` | 数据库连接串 |
| `SECRET` | 自动生成并持久化 | token 签名密钥（`backend/data/auth_secret`） |

## 运行测试

```bash
cd backend
python -m unittest discover tests
```

## 使用说明

1. 浏览器打开前端，用初始管理员账号登录
2. 「抖音账号」页添加账号 → 弹出扫码二维码 → 手机抖音扫码确认
3. 「续火任务」新建任务：填写目标用户（昵称/抖音号，支持逗号分隔多个与别名）、选择发送账号、设置执行时间
4. 「运行记录」查看每次执行结果；账号 Cookie 临近过期会自动续期，彻底失效会在列表标红并可选企业微信通知

## 注意事项

- 扫码登录与消息发送依赖 Playwright + Chromium，首次使用请执行 `playwright install chromium`
- 无头浏览器内存占用较高（单实例 200MB+），低配服务器注意并发数
- 本项目仅供学习交流，请遵守抖音用户协议，合理控制发送频率

## License

MIT
