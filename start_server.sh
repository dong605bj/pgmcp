#!/bin/bash
# pg-mcp MCP 服务启动脚本 (stdio 传输)
# 客户端 (Claude Code / Claude Desktop / MCP Inspector 等) 将本脚本作为 stdio 命令拉起即可。
#
# 优先级: 客户端注入的环境变量 > 项目 .env 文件

cd "$(dirname "$0")"

# 将 .env 导出到进程环境, 但不覆盖客户端已显式注入的变量
while IFS='=' read -r key value; do
    case "$key" in
        \#*|"") continue ;;                      # 跳过注释和空行
    esac
    key="$(echo "$key" | tr -d '[:space:]')"
    if [ -z "${!key+x}" ]; then                  # 仅当环境中未定义时才导出
        export "$key=$value"
    fi
done < .env

export PYTHONPATH="$(pwd)/src"
exec /root/miniconda3/envs/peixun/bin/python -m pg_mcp
