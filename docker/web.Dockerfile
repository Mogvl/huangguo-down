# 前端镜像：nginx 托管静态页，并把 API 反代到后端
FROM nginx:1.27-alpine

# 静态页
COPY public/ /usr/share/nginx/html/

# 站点配置（含 API 反代规则）
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf

# 容器内统一监听 80，由 compose 映射到宿主机 7731
EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -q --spider http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
