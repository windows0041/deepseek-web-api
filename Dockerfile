# DeepSeek Chat 网页账号 OpenAI 兼容网关
# 构建:  docker build -t deepseek-chat-api .
# 运行:  docker run -d -p 8000:8000 -e DS_TOKEN="你的token" --name ds-gateway deepseek-chat-api
# 端点:  http://<host>:8000/v1  (OpenAI 兼容; OPENAI_API_KEY 任意值)

FROM python:3.13-slim

# 纯标准库项目, 无需 pip 安装
WORKDIR /app
COPY deepseek_chat.py deepseek_pow.py deepseek_openai.py ./

# 账号凭证走环境变量注入(不想进镜像层就用 --env-file 或运行时 -e)
ENV DS_TOKEN="" \
    DS_COOKIE="" \
    GATEWAY_HOST="0.0.0.0" \
    GATEWAY_PORT="8000" \
    DEFAULT_SESSION="latest" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# 容器内监听 0.0.0.0; 会话复用模式由 DEFAULT_SESSION 控制
# (latest=复用最近网页会话, 固定 ID=传该 ID, 空=每次新建)
CMD ["sh", "-c", "python3 deepseek_openai.py --host \"$GATEWAY_HOST\" --port \"$GATEWAY_PORT\" ${DEFAULT_SESSION:+--default-session \"$DEFAULT_SESSION\"}"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3).status==200 else 1)"
