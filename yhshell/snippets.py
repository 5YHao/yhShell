"""Built-in command snippets (指令集) for the sidebar and terminal palette.

The library is deliberately static: a curated set of everyday commands with
Kubernetes first, followed by Docker, Linux inspection, networking, logs,
services, ZMODEM transfers and Git.

A value the operator has to fill in is written as ``<变量名>``.  The frontend
turns every placeholder into an input field; leaving a field empty keeps the
literal ``<变量名>`` in the command so it can still be edited in the terminal.
Insertion only writes the text into the terminal's input line — it never sends
Enter.
"""

from __future__ import annotations

import re
from typing import Any

# Placeholders are short and never span lines.  Chinese names are supported.
VARIABLE_RE = re.compile(r"<([^<>\r\n]{1,40})>")
MAX_COMMAND_LENGTH = 400

# Snippets pinned to the "常用" quick bar at the bottom of the terminal window.
# The quick bar displays these in library group order.
FAVORITE_IDS: tuple[str, ...] = (
    "k8s-pods-all",
    "k8s-nodes",
    "k8s-logs",
    "k8s-logs-tail",
    "k8s-exec",
    "k8s-describe-pod",
    "k8s-rollout-restart",
    "k8s-port-forward",
    "docker-ps",
    "docker-logs",
    "linux-free",
    "linux-df",
    "net-ss-listen",
    "log-tail-f",
    "svc-status",
    "git-status",
)
FAVORITE_SET = frozenset(FAVORITE_IDS)


def extract_variables(template: str) -> list[str]:
    """Return the placeholders of *template* in first-seen order, deduplicated."""
    variables: list[str] = []
    for raw in VARIABLE_RE.findall(str(template or "")):
        name = raw.strip()
        if name and name not in variables:
            variables.append(name)
    return variables


def render_command(template: str, values: dict[str, Any] | None = None) -> str:
    """Replace filled placeholders; blank values keep ``<变量名>`` untouched."""
    command = str(template or "")
    values = values or {}
    for name in extract_variables(command):
        value = str(values.get(name) or "").strip()
        if value:
            command = command.replace(f"<{name}>", value)
    return command


def public_groups() -> list[dict[str, Any]]:
    """Build a fresh, JSON-serialisable snapshot of the built-in library.

    A new list is returned on every call so the frontend payload can never
    mutate the module-level definitions (and tests can clear a returned list).
    """
    groups: list[dict[str, Any]] = []
    for group in SNIPPET_GROUPS:
        items: list[dict[str, Any]] = []
        for item in group["items"]:
            command = item["command"]
            items.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "command": command,
                    "note": item.get("note") or "",
                    "variables": extract_variables(command),
                    "favorite": item["id"] in FAVORITE_SET,
                }
            )
        groups.append(
            {
                "id": group["id"],
                "name": group["name"],
                "description": group.get("description") or "",
                "items": items,
            }
        )
    return groups


def snippet_total(groups: list[dict[str, Any]] | None = None) -> int:
    """Total number of snippets in *groups* (or the full library)."""
    return sum(len(group.get("items") or []) for group in (groups if groups is not None else public_groups()))


def merged_groups(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return the built-in library with the user's edits applied.

    *config* is the ``snippets`` section of the config file: custom groups with
    their own commands, ``overrides`` patching single built-in commands and
    ``hidden`` listing removed built-ins.  The result is the catalogue the UI
    works with — every command carries ``builtin``/``edited`` flags so the
    frontend can tell the library apart from the user's additions.
    """
    config = config if isinstance(config, dict) else {}
    overrides = config.get("overrides") if isinstance(config.get("overrides"), dict) else {}
    hidden = set(config.get("hidden") or [])

    groups: list[dict[str, Any]] = []
    for group in public_groups():
        items: list[dict[str, Any]] = []
        for item in group["items"]:
            if item["id"] in hidden:
                continue
            patch = overrides.get(item["id"])
            merged = dict(item)
            merged["builtin"] = True
            merged["edited"] = False
            if isinstance(patch, dict):
                for field in ("name", "command", "note"):
                    value = str(patch.get(field) or "").strip()
                    # An explicitly empty note clears the library's note;
                    # missing fields still inherit their built-in values.
                    if value or (field == "note" and field in patch):
                        merged[field] = value
                merged["variables"] = extract_variables(merged["command"])
                merged["edited"] = True
            items.append(merged)
        groups.append(
            {
                "id": group["id"],
                "name": group["name"],
                "description": group["description"],
                "builtin": True,
                "items": items,
            }
        )

    for group in config.get("groups") or []:
        if not isinstance(group, dict):
            continue
        items = []
        for item in group.get("items") or []:
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or "")
            items.append(
                {
                    "id": item.get("id") or "",
                    "name": item.get("name") or command[:40],
                    "command": command,
                    "note": item.get("note") or "",
                    "variables": extract_variables(command),
                    "favorite": False,
                    "builtin": False,
                    "edited": False,
                }
            )
        groups.append(
            {
                "id": group.get("id") or "",
                "name": group.get("name") or "我的指令",
                "description": group.get("description") or "",
                "builtin": False,
                "items": items,
            }
        )
    return groups


SNIPPET_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "id": "k8s-query",
        "name": "Kubernetes 查看",
        "description": "kubectl 查询、日志与排障",
        "items": (
            {
                "id": "k8s-pods-all",
                "name": "查看所有 Pod",
                "command": "kubectl get pods -A -o wide",
                "note": "全命名空间，含节点与 Pod IP",
            },
            {
                "id": "k8s-pods-ns",
                "name": "查看命名空间 Pod",
                "command": "kubectl get pods -n <命名空间> -o wide",
                "note": "指定命名空间，含节点与 Pod IP",
            },
            {
                "id": "k8s-namespaces",
                "name": "查看命名空间",
                "command": "kubectl get namespaces",
                "note": "列出所有 Namespace",
            },
            {
                "id": "k8s-nodes",
                "name": "查看节点",
                "command": "kubectl get nodes -o wide",
                "note": "节点状态、版本、内网 IP",
            },
            {
                "id": "k8s-all",
                "name": "查看命名空间全部资源",
                "command": "kubectl get all -n <命名空间>",
                "note": "Pod / Service / Deployment / StatefulSet 等",
            },
            {
                "id": "k8s-deploy",
                "name": "查看 Deployment",
                "command": "kubectl get deploy -n <命名空间> -o wide",
                "note": "副本数与可用副本",
            },
            {
                "id": "k8s-svc",
                "name": "查看 Service",
                "command": "kubectl get svc -n <命名空间> -o wide",
                "note": "ClusterIP / NodePort / LoadBalancer",
            },
            {
                "id": "k8s-ingress",
                "name": "查看 Ingress",
                "command": "kubectl get ingress -n <命名空间>",
                "note": "域名与后端 Service",
            },
            {
                "id": "k8s-cm",
                "name": "查看 ConfigMap",
                "command": "kubectl get configmap -n <命名空间>",
                "note": "配置项清单，不含明文内容",
            },
            {
                "id": "k8s-secret",
                "name": "查看 Secret",
                "command": "kubectl get secret -n <命名空间>",
                "note": "只列名称与类型",
            },
            {
                "id": "k8s-pvc",
                "name": "查看 PVC",
                "command": "kubectl get pvc -n <命名空间>",
                "note": "持久卷声明与绑定状态",
            },
            {
                "id": "k8s-pv",
                "name": "查看 PV",
                "command": "kubectl get pv",
                "note": "集群级持久卷与回收策略",
            },
            {
                "id": "k8s-endpoints",
                "name": "查看 Endpoints",
                "command": "kubectl get endpoints -n <命名空间>",
                "note": "Service 实际转发的 Pod IP",
            },
            {
                "id": "k8s-events",
                "name": "查看事件",
                "command": "kubectl get events -n <命名空间> --sort-by=.lastTimestamp",
                "note": "按时间排序的调度 / 拉镜像 / 探针事件",
            },
            {
                "id": "k8s-abnormal-pods",
                "name": "查看异常 Pod",
                "command": "kubectl get pods -A --field-selector=status.phase!=Running",
                "note": "非 Running 状态的 Pod",
            },
            {
                "id": "k8s-describe-pod",
                "name": "查看 Pod 详情",
                "command": "kubectl describe pod <Pod名> -n <命名空间>",
                "note": "事件、探针、污点与容器状态",
            },
            {
                "id": "k8s-logs",
                "name": "查看 Pod 日志",
                "command": "kubectl logs -f <Pod名> -n <命名空间>",
                "note": "持续跟随输出，Ctrl+C 停止",
            },
            {
                "id": "k8s-logs-previous",
                "name": "查看上次崩溃日志",
                "command": "kubectl logs <Pod名> -n <命名空间> --previous",
                "note": "容器重启前的最后一份日志",
            },
            {
                "id": "k8s-logs-container",
                "name": "查看指定容器日志",
                "command": "kubectl logs -f <Pod名> -c <容器名> -n <命名空间>",
                "note": "多容器 Pod 指定容器",
            },
            {
                "id": "k8s-logs-tail",
                "name": "查看最近 200 行日志",
                "command": "kubectl logs --tail=200 <Pod名> -n <命名空间>",
                "note": "不跟随输出，适合快速回看",
            },
            {
                "id": "k8s-exec",
                "name": "进入 Pod 容器",
                "command": "kubectl exec -it <Pod名> -n <命名空间> -- /bin/bash",
                "note": "镜像没有 bash 时换成 /bin/sh",
            },
            {
                "id": "k8s-exec-cmd",
                "name": "在容器内执行命令",
                "command": "kubectl exec -it <Pod名> -n <命名空间> -- <命令>",
                "note": "例如 -- ls -alh /app",
            },
            {
                "id": "k8s-top-pod",
                "name": "查看 Pod 资源占用",
                "command": "kubectl top pod -n <命名空间>",
                "note": "需要 metrics-server",
            },
            {
                "id": "k8s-top-node",
                "name": "查看节点资源占用",
                "command": "kubectl top node",
                "note": "需要 metrics-server",
            },
            {
                "id": "k8s-pods-label",
                "name": "按标签筛选 Pod",
                "command": "kubectl get pods -n <命名空间> -l <标签选择器> -o wide",
                "note": "例如 -l app=nginx,tier=web",
            },
            {
                "id": "k8s-pod-yaml",
                "name": "查看 Pod YAML",
                "command": "kubectl get pod <Pod名> -n <命名空间> -o yaml",
                "note": "完整对象定义与状态",
            },
            {
                "id": "k8s-images",
                "name": "查看正在使用的镜像",
                "command": "kubectl get pods -n <命名空间> -o jsonpath='{.items[*].spec.containers[*].image}'",
                "note": "排查镜像版本是否一致",
            },
            {
                "id": "k8s-api-resources",
                "name": "查看 API 资源",
                "command": "kubectl api-resources",
                "note": "查看资源名与简写（如 deploy / svc）",
            },
            {
                "id": "k8s-current-context",
                "name": "查看当前上下文",
                "command": "kubectl config current-context",
                "note": "确认当前连的是哪个集群",
            },
            {
                "id": "k8s-contexts",
                "name": "查看所有上下文",
                "command": "kubectl config get-contexts",
                "note": "切换集群前的清单",
            },
        ),
    },
    {
        "id": "k8s-ops",
        "name": "Kubernetes 操作",
        "description": "发布、扩缩容、节点维护与文件复制",
        "items": (
            {
                "id": "k8s-apply",
                "name": "应用 YAML",
                "command": "kubectl apply -f <文件路径>",
                "note": "创建或更新资源",
            },
            {
                "id": "k8s-delete-file",
                "name": "删除 YAML 中的资源",
                "command": "kubectl delete -f <文件路径>",
                "note": "删除前先确认文件内容",
            },
            {
                "id": "k8s-rollout-restart",
                "name": "重启 Deployment",
                "command": "kubectl rollout restart deployment/<Deployment名> -n <命名空间>",
                "note": "滚动重启，不中断服务",
            },
            {
                "id": "k8s-rollout-status",
                "name": "查看发布状态",
                "command": "kubectl rollout status deployment/<Deployment名> -n <命名空间>",
                "note": "等待并显示滚动发布进度",
            },
            {
                "id": "k8s-rollout-undo",
                "name": "回滚 Deployment",
                "command": "kubectl rollout undo deployment/<Deployment名> -n <命名空间>",
                "note": "回到上一个 ReplicaSet",
            },
            {
                "id": "k8s-scale",
                "name": "扩缩容 Deployment",
                "command": "kubectl scale deployment/<Deployment名> --replicas=<副本数> -n <命名空间>",
                "note": "临时调整副本数量",
            },
            {
                "id": "k8s-edit",
                "name": "编辑 Deployment",
                "command": "kubectl edit deployment/<Deployment名> -n <命名空间>",
                "note": "保存后自动触发滚动更新",
            },
            {
                "id": "k8s-delete-pod",
                "name": "删除 Pod",
                "command": "kubectl delete pod <Pod名> -n <命名空间>",
                "note": "由控制器管理的 Pod 会被重建",
            },
            {
                "id": "k8s-delete-pod-force",
                "name": "强制删除 Pod",
                "command": "kubectl delete pod <Pod名> -n <命名空间> --force --grace-period=0",
                "note": "仅在 Pod 卡在 Terminating 时使用",
            },
            {
                "id": "k8s-port-forward",
                "name": "端口转发",
                "command": "kubectl port-forward <Pod名> <本地端口>:<远端端口> -n <命名空间>",
                "note": "本地访问集群内服务，Ctrl+C 结束",
            },
            {
                "id": "k8s-cp-out",
                "name": "从容器复制文件到本地",
                "command": "kubectl cp <命名空间>/<Pod名>:<容器内路径> <本地路径>",
                "note": "需要容器内有 tar",
            },
            {
                "id": "k8s-cp-in",
                "name": "复制本地文件到容器",
                "command": "kubectl cp <本地路径> <命名空间>/<Pod名>:<容器内路径>",
                "note": "需要容器内有 tar",
            },
            {
                "id": "k8s-cordon",
                "name": "隔离节点",
                "command": "kubectl cordon <节点名>",
                "note": "标记不可调度，不驱逐存量 Pod",
            },
            {
                "id": "k8s-drain",
                "name": "驱逐节点 Pod",
                "command": "kubectl drain <节点名> --ignore-daemonsets --delete-emptydir-data",
                "note": "节点维护前使用，会驱逐存量 Pod",
            },
            {
                "id": "k8s-uncordon",
                "name": "恢复节点调度",
                "command": "kubectl uncordon <节点名>",
                "note": "维护完成后恢复",
            },
            {
                "id": "k8s-node-labels",
                "name": "查看节点标签",
                "command": "kubectl get nodes --show-labels",
                "note": "调度约束与节点池标记",
            },
            {
                "id": "k8s-debug-pod",
                "name": "启动临时调试 Pod",
                "command": "kubectl run <Pod名> --image=<镜像> -it --rm --restart=Never -- /bin/sh",
                "note": "退出后自动删除",
            },
            {
                "id": "k8s-use-context",
                "name": "切换集群上下文",
                "command": "kubectl config use-context <上下文名>",
                "note": "危险：确认切到的目标集群",
            },
        ),
    },
    {
        "id": "docker",
        "name": "Docker",
        "description": "容器、镜像与 Compose 常用命令",
        "items": (
            {
                "id": "docker-ps",
                "name": "运行中的容器",
                "command": "docker ps",
                "note": "含端口映射与启动时间",
            },
            {
                "id": "docker-ps-all",
                "name": "所有容器",
                "command": "docker ps -a",
                "note": "包含已退出的容器与退出码",
            },
            {
                "id": "docker-images",
                "name": "镜像列表",
                "command": "docker images",
                "note": "镜像、标签与体积",
            },
            {
                "id": "docker-logs",
                "name": "容器日志",
                "command": "docker logs -f --tail=200 <容器名>",
                "note": "持续跟随最近 200 行",
            },
            {
                "id": "docker-exec",
                "name": "进入容器",
                "command": "docker exec -it <容器名> /bin/sh",
                "note": "镜像有 bash 时可换成 /bin/bash",
            },
            {
                "id": "docker-inspect",
                "name": "容器详情",
                "command": "docker inspect <容器名>",
                "note": "JSON 格式，含网络与挂载",
            },
            {
                "id": "docker-stats",
                "name": "容器资源占用",
                "command": "docker stats --no-stream",
                "note": "CPU / 内存 / 网络 / 磁盘 IO",
            },
            {
                "id": "docker-restart",
                "name": "重启容器",
                "command": "docker restart <容器名>",
                "note": "重启后检查日志是否恢复",
            },
            {
                "id": "docker-system-df",
                "name": "Docker 磁盘占用",
                "command": "docker system df",
                "note": "镜像、容器、卷与缓存占用",
            },
            {
                "id": "docker-compose-ps",
                "name": "Compose 服务状态",
                "command": "docker compose ps",
                "note": "在 compose.yaml 所在目录执行",
            },
            {
                "id": "docker-compose-logs",
                "name": "Compose 日志",
                "command": "docker compose logs -f --tail=200",
                "note": "跟随全部服务日志",
            },
            {
                "id": "docker-compose-up",
                "name": "Compose 启动/更新",
                "command": "docker compose up -d",
                "note": "后台启动，按需重建容器",
            },
            {
                "id": "docker-networks",
                "name": "网络列表",
                "command": "docker network ls",
                "note": "查看 bridge / host / overlay",
            },
            {
                "id": "docker-volumes",
                "name": "数据卷列表",
                "command": "docker volume ls",
                "note": "清理前确认是否有数据",
            },
        ),
    },
    {
        "id": "linux",
        "name": "Linux 巡检",
        "description": "负载、内存、磁盘与登录情况",
        "items": (
            {
                "id": "linux-uptime",
                "name": "系统负载",
                "command": "uptime",
                "note": "1 / 5 / 15 分钟平均负载",
            },
            {
                "id": "linux-free",
                "name": "内存使用",
                "command": "free -h",
                "note": "注意 available 而不是 free",
            },
            {
                "id": "linux-df",
                "name": "磁盘使用",
                "command": "df -hT",
                "note": "含文件系统类型",
            },
            {
                "id": "linux-df-inode",
                "name": "inode 使用",
                "command": "df -i",
                "note": "磁盘有空间却写不进去时检查",
            },
            {
                "id": "linux-du",
                "name": "目录占用 Top 20",
                "command": "du -sh <目录>/* | sort -rh | head -n 20",
                "note": "定位大目录",
            },
            {
                "id": "linux-ps-cpu",
                "name": "CPU 前 10 进程",
                "command": "ps aux --sort=-%cpu | head -n 11",
                "note": "按 CPU 使用率倒序",
            },
            {
                "id": "linux-ps-mem",
                "name": "内存前 10 进程",
                "command": "ps aux --sort=-%mem | head -n 11",
                "note": "按内存使用率倒序",
            },
            {
                "id": "linux-top",
                "name": "实时进程监控",
                "command": "top",
                "note": "按 q 退出",
            },
            {
                "id": "linux-os-release",
                "name": "系统版本",
                "command": "cat /etc/os-release",
                "note": "发行版与版本号",
            },
            {
                "id": "linux-uname",
                "name": "内核版本",
                "command": "uname -a",
                "note": "内核、架构与主机名",
            },
            {
                "id": "linux-who",
                "name": "当前登录用户",
                "command": "who",
                "note": "来源 IP 与登录时间",
            },
            {
                "id": "linux-last",
                "name": "最近登录记录",
                "command": "last -n 20",
                "note": "排查异常登录",
            },
            {
                "id": "linux-dmesg",
                "name": "内核日志",
                "command": "dmesg | tail -n 50",
                "note": "OOM、磁盘与网卡告警",
            },
            {
                "id": "linux-journal",
                "name": "系统日志（最近）",
                "command": "journalctl -xe --no-pager",
                "note": "systemd 日志与失败原因",
            },
            {
                "id": "linux-crontab",
                "name": "当前用户定时任务",
                "command": "crontab -l",
                "note": "查看当前用户的 cron",
            },
            {
                "id": "linux-timedate",
                "name": "时间与同步状态",
                "command": "timedatectl",
                "note": "时区、NTP 同步",
            },
            {
                "id": "linux-last-boot",
                "name": "上次启动时间",
                "command": "who -b",
                "note": "确认是否意外重启",
            },
        ),
    },
    {
        "id": "network",
        "name": "网络排查",
        "description": "端口、连通性、DNS 与抓包",
        "items": (
            {
                "id": "net-ss-listen",
                "name": "监听端口",
                "command": "ss -tulnp",
                "note": "TCP / UDP 监听与进程",
            },
            {
                "id": "net-ss-conn",
                "name": "TCP 连接状态",
                "command": "ss -antp",
                "note": "排查大量 TIME_WAIT / CLOSE_WAIT",
            },
            {
                "id": "net-ip-addr",
                "name": "网卡地址",
                "command": "ip addr",
                "note": "IP、掩码与网卡状态",
            },
            {
                "id": "net-ip-route",
                "name": "路由表",
                "command": "ip route",
                "note": "默认网关与多网卡路由",
            },
            {
                "id": "net-ping",
                "name": "测试连通性",
                "command": "ping -c 4 <主机>",
                "note": "发 4 个包后停止",
            },
            {
                "id": "net-traceroute",
                "name": "路由追踪",
                "command": "traceroute <主机>",
                "note": "需安装 traceroute",
            },
            {
                "id": "net-curl-head",
                "name": "HTTP 响应头",
                "command": "curl -I <URL>",
                "note": "查看状态码、重定向与头信息",
            },
            {
                "id": "net-curl-time",
                "name": "HTTP 状态与耗时",
                "command": "curl -s -o /dev/null -w '%{http_code} %{time_total}s' <URL>",
                "note": "排查接口慢与超时",
            },
            {
                "id": "net-dig",
                "name": "DNS 解析",
                "command": "dig +short <域名>",
                "note": "只显示解析结果",
            },
            {
                "id": "net-nslookup",
                "name": "DNS 查询（nslookup）",
                "command": "nslookup <域名>",
                "note": "没有 dig 时使用",
            },
            {
                "id": "net-nc",
                "name": "端口探测",
                "command": "nc -zv <主机> <端口>",
                "note": "需安装 netcat",
            },
            {
                "id": "net-tcpdump",
                "name": "抓包",
                "command": "tcpdump -i any -nn port <端口>",
                "note": "Ctrl+C 停止，需要 root",
            },
            {
                "id": "net-iptables",
                "name": "防火墙规则",
                "command": "iptables -L -n -v",
                "note": "需要 root；nftables 用 nft list ruleset",
            },
            {
                "id": "net-mtr",
                "name": "链路质量",
                "command": "mtr -rw <主机>",
                "note": "结合丢包与延迟，需安装 mtr",
            },
        ),
    },
    {
        "id": "logs",
        "name": "日志与文件",
        "description": "查看日志、搜索、打包与权限",
        "items": (
            {
                "id": "log-tail-f",
                "name": "实时查看日志",
                "command": "tail -f <日志文件>",
                "note": "Ctrl+C 停止",
            },
            {
                "id": "log-tail-n",
                "name": "查看最近 200 行",
                "command": "tail -n 200 <日志文件>",
                "note": "快速回看",
            },
            {
                "id": "log-grep",
                "name": "搜索日志关键字",
                "command": "grep -n '<关键字>' <日志文件>",
                "note": "显示行号",
            },
            {
                "id": "log-grep-r",
                "name": "递归搜索目录",
                "command": "grep -rn '<关键字>' <目录>",
                "note": "忽略二进制文件可加 -I",
            },
            {
                "id": "log-journal-unit",
                "name": "服务日志（journalctl）",
                "command": "journalctl -u <服务名> -f --no-pager",
                "note": "持续跟随指定服务",
            },
            {
                "id": "log-find",
                "name": "按名称查找文件",
                "command": "find <目录> -type f -name '<文件名模式>'",
                "note": "例如 '*.log'",
            },
            {
                "id": "log-find-recent",
                "name": "最近一天修改的文件",
                "command": "find <目录> -type f -mtime -1",
                "note": "定位新产生的文件",
            },
            {
                "id": "log-du",
                "name": "目录总大小",
                "command": "du -sh <目录>",
                "note": "统计单个目录",
            },
            {
                "id": "log-tar-create",
                "name": "打包压缩",
                "command": "tar -czf <压缩包名>.tar.gz <目录>",
                "note": "gzip 压缩",
            },
            {
                "id": "log-tar-extract",
                "name": "解压 tar.gz",
                "command": "tar -xzf <压缩包名>.tar.gz",
                "note": "解压到当前目录",
            },
            {
                "id": "log-ls",
                "name": "查看目录内容",
                "command": "ls -alh <目录>",
                "note": "含隐藏文件与人类可读大小",
            },
            {
                "id": "log-sed",
                "name": "批量替换文本",
                "command": "sed -i 's/<原文本>/<新文本>/g' <文件路径>",
                "note": "先备份或先不加 -i 预览",
            },
            {
                "id": "log-chmod",
                "name": "修改权限",
                "command": "chmod -R <权限> <目录>",
                "note": "例如 755 或 u+rwX",
            },
            {
                "id": "log-chown",
                "name": "修改属主",
                "command": "chown -R <用户>:<用户组> <目录>",
                "note": "需要 root",
            },
            {
                "id": "log-rsync",
                "name": "同步目录",
                "command": "rsync -avz <源目录>/ <目标目录>/",
                "note": "源目录末尾的 / 表示同步内容",
            },
        ),
    },
    {
        "id": "service",
        "name": "进程与服务",
        "description": "查进程、占端口、systemd 操作",
        "items": (
            {
                "id": "svc-ps-grep",
                "name": "按关键字查进程",
                "command": "ps aux | grep <关键字>",
                "note": "grep 本身也会出现在结果里",
            },
            {
                "id": "svc-pgrep",
                "name": "按名称查进程",
                "command": "pgrep -af <关键字>",
                "note": "只输出 PID 与完整命令行",
            },
            {
                "id": "svc-kill",
                "name": "强制结束进程",
                "command": "kill -9 <PID>",
                "note": "先尝试 kill -15 优雅退出",
            },
            {
                "id": "svc-lsof",
                "name": "查看端口占用",
                "command": "lsof -i :<端口>",
                "note": "需要 lsof",
            },
            {
                "id": "svc-status",
                "name": "服务状态",
                "command": "systemctl status <服务名> --no-pager",
                "note": "状态、主 PID 与最近日志",
            },
            {
                "id": "svc-restart",
                "name": "重启服务",
                "command": "systemctl restart <服务名>",
                "note": "重启后立即看状态与日志",
            },
            {
                "id": "svc-enable",
                "name": "设置开机自启并启动",
                "command": "systemctl enable --now <服务名>",
                "note": "enable + start",
            },
            {
                "id": "svc-failed",
                "name": "查看失败的服务",
                "command": "systemctl --failed --no-pager",
                "note": "巡检必看",
            },
            {
                "id": "svc-running",
                "name": "运行中的服务",
                "command": "systemctl list-units --type=service --state=running --no-pager",
                "note": "当前活跃服务清单",
            },
        ),
    },
    {
        "id": "transfer",
        "name": "文件传输",
        "description": "yhShell 内置的 ZMODEM（rz / sz）",
        "items": (
            {
                "id": "transfer-download",
                "name": "下载文件到本地",
                "command": "sz <文件路径>",
                "note": "远端执行，yhShell 自动接收",
            },
            {
                "id": "transfer-download-multi",
                "name": "下载多个文件",
                "command": "sz <文件路径1> <文件路径2>",
                "note": "也支持通配符，如 *.log",
            },
            {
                "id": "transfer-upload",
                "name": "上传文件（覆盖已存在）",
                "command": "rz -y",
                "note": "会直接覆盖远端同名文件",
            },
            {
                "id": "transfer-upload-ask",
                "name": "上传文件（询问覆盖）",
                "command": "rz",
                "note": "远端会等待本地选择文件",
            },
        ),
    },
    {
        "id": "git",
        "name": "Git 常用",
        "description": "状态、日志、分支与拉取",
        "items": (
            {
                "id": "git-status",
                "name": "工作区状态",
                "command": "git status",
                "note": "改动与未跟踪文件",
            },
            {
                "id": "git-log",
                "name": "提交历史",
                "command": "git log --oneline --graph -n 15",
                "note": "最近 15 条，含分支图",
            },
            {
                "id": "git-pull",
                "name": "拉取并变基",
                "command": "git pull --rebase",
                "note": "避免产生多余 merge 提交",
            },
            {
                "id": "git-diff",
                "name": "变更统计",
                "command": "git diff --stat",
                "note": "按文件查看增删行数",
            },
            {
                "id": "git-branch",
                "name": "分支列表",
                "command": "git branch -a",
                "note": "含远程分支",
            },
            {
                "id": "git-checkout",
                "name": "切换分支",
                "command": "git checkout <分支名>",
                "note": "有未提交改动时先 stash",
            },
            {
                "id": "git-remote",
                "name": "查看远程仓库",
                "command": "git remote -v",
                "note": "确认推送地址",
            },
        ),
    },
)
