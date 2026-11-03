# 后端镜像：Node 运行 Worker 解析逻辑 + 下载接口
FROM node:20-alpine

# 只装运行时必需的东西；本项目零 npm 依赖，不需要 npm install
WORKDIR /app

# 先拷代码再设用户，利用层缓存
COPY worker/  ./worker/
COPY lib/     ./lib/
COPY server.js ./server.js
COPY package.json ./package.json

# 用非 root 运行
RUN addgroup -g 1001 -S app \
 && adduser  -u 1001 -S app -G app \
 && chown -R app:app /app
USER app

ENV NODE_ENV=production \
    PORT=7732 \
    HOST=0.0.0.0 \
    SERVE_STATIC=0

EXPOSE 7732

# 健康检查：/healthz 由 server.js 直接返回，不依赖上游站点
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:7732/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
