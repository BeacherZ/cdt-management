#!/usr/bin/env bash
# CDT-Management 一键安装（内部走 docker run，不需要 docker compose 插件）
#
#   curl -fsSL https://raw.githubusercontent.com/BeacherZ/cdt-management/main/install.sh | bash
#
# 可覆盖的变量：
#   PORT=18282            宿主机端口，默认 8282
#   DIR=/srv/cdt          数据目录，默认 /opt/docker/cdt-management
#   NO_DOCKER_INSTALL=1   没装 Docker 时不要自动装，直接报错退出
#   FORCE=1               已是最新版本也强制重建容器
#   UNINSTALL=1           卸载本项目（容器、镜像、数据目录一起删，要确认）
#   UNINSTALL=docker      连 Docker 一起卸
#
# 已装过时重跑同一条命令，会先在终端里选：1 检查更新 / 2 卸载 / 3 卸载并卸 Docker。
# 非交互环境（没有终端）不询问，直接比对版本升级。
set -euo pipefail

IMAGE="ghcr.io/beacherz/cdt-management:latest"
NAME="cdt-management"
PORT="${PORT:-8282}"
DIR="${DIR:-/opt/docker/cdt-management}"
VER_LABEL="cn.cdt-management.version"

say() { printf '\033[32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die() { printf '\033[31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

image_version() {
    docker image inspect -f "{{index .Config.Labels \"$VER_LABEL\"}}" "$IMAGE" 2>/dev/null || true
}

container_version() {
    docker inspect -f "{{index .Config.Labels \"$VER_LABEL\"}}" "$NAME" 2>/dev/null || true
}

public_ip() {
    local ip u
    for u in https://api.ipify.org https://ifconfig.me/ip https://ipinfo.io/ip; do
        ip="$(curl -fsS --max-time 4 "$u" 2>/dev/null)" || continue
        ip="$(printf '%s' "$ip" | tr -d '[:space:]')"
        case "$ip" in
            "" | *[!0-9.]*) continue ;;
            *.*.*.*) printf '%s\n' "$ip"; return 0 ;;
        esac
    done
    return 1
}

local_ip() {
    local out ip
    out="$(ip -4 route get 1.1.1.1 2>/dev/null)" || true
    ip="$(printf '%s\n' "$out" | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)"
    [ -n "$ip" ] || ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    printf '%s\n' "${ip:-127.0.0.1}"
}

show_url() {
    local pub lan
    pub="$(public_ip || true)"
    lan="$(local_ip)"
    say "内网：http://${lan}:${PORT}"
    if [ -n "$pub" ]; then
        say "公网：http://${pub}:${PORT}"
    fi
    URL_SHOWN=1
}

remove_project() {
    if command -v docker >/dev/null 2>&1; then
        say "删除容器 $NAME"
        docker rm -f "$NAME" >/dev/null 2>&1 || true
        say "删除镜像 $IMAGE"
        docker rmi "$IMAGE" >/dev/null 2>&1 || true
    else
        say "没装 Docker，跳过容器和镜像"
    fi
    say "删除数据目录 $DIR"
    rm -rf "$DIR"
}

tty_ok() { (: < /dev/tty) 2>/dev/null; }

confirm_or_die() {
    [ "${CONFIRM:-}" = "yes" ] && return 0
    if tty_ok; then
        printf '确认请输入 yes：'
        answer=""
        read -r answer 2>/dev/null < /dev/tty || answer=""
        [ "$answer" = "yes" ] || die "已取消，什么都没删"
    else
        die "非交互环境：确认请加 CONFIRM=yes"
    fi
}

have_project() {
    command -v docker >/dev/null 2>&1 || return 1
    docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$NAME"
}

ask_action() {
    local choice=""
    printf '\n'
    say "检测到已安装 $NAME"
    show_url
    printf '\n'
    printf '  1) 检查更新（默认）\n  2) 卸载本项目：容器、镜像和数据一起删\n  3) 全部卸载：连 Docker 也一起卸\n'
    printf '请选择 [1-3]，10 秒不选默认 1：'
    read -r -t 10 choice 2>/dev/null < /dev/tty || choice=""
    case "$choice" in
        2)
            printf '\n'
            say "会删掉容器、镜像和数据目录 $DIR，删了没法恢复"
            confirm_or_die
            remove_project
            exit 0
            ;;
        3)
            uninstall_docker
            exit 0
            ;;
    esac
}

uninstall_docker() {
    printf '\n'
    say "接下来会卸载 Docker 引擎并删除 /var/lib/docker"
    say "本机所有容器、镜像、卷都会没，不只是本项目"
    say "本项目的数据目录 $DIR 也会一起删，删了没法恢复"
    confirm_or_die
    remove_project
    say "卸载 Docker 引擎"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get purge -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
            docker-compose-plugin docker.io docker-doc docker-compose podman-docker containerd runc \
            >/dev/null 2>&1 || true
        apt-get autoremove -y >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1; then
        dnf remove -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
            docker-compose-plugin docker.io >/dev/null 2>&1 || true
    elif command -v yum >/dev/null 2>&1; then
        yum remove -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
            docker-compose-plugin docker.io >/dev/null 2>&1 || true
    else
        say "没认出包管理器，Docker 引擎需要手动卸"
    fi
    rm -rf /var/lib/docker /var/lib/containerd
    say "Docker 已卸载"
}

# 装/卸 Docker 和写数据目录都要 root
[ "$(id -u)" -eq 0 ] ||
    die "请用 root 运行：curl -fsSL <本脚本地址> | sudo bash"

case "${UNINSTALL:-0}" in
    0) ;;
    1 | project | yes)
        say "卸载 CDT-Management"
        say "会删掉容器、镜像和数据目录 $DIR，删了没法恢复"
        confirm_or_die
        remove_project
        exit 0
        ;;
    docker | all)
        say "卸载 CDT-Management 和 Docker"
        uninstall_docker
        exit 0
        ;;
    *)
        die "UNINSTALL 只认 1 或 docker"
        ;;
esac

if [ "${FORCE:-0}" != "1" ] && have_project && tty_ok; then
    ask_action
fi

if ! command -v docker >/dev/null 2>&1; then
    if [ "${NO_DOCKER_INSTALL:-0}" = "1" ]; then
        die "没装 Docker。手动装：https://docs.docker.com/engine/install/"
    fi
    say "没检测到 Docker，开始自动安装（官方脚本）"
    curl -fsSL https://get.docker.com | sh ||
        die "Docker 自动安装失败。手动装：https://docs.docker.com/engine/install/"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable --now docker >/dev/null 2>&1 || true
    fi
    command -v docker >/dev/null 2>&1 ||
        die "装完了但找不到 docker 命令，重开终端再跑一次。"
fi

docker info >/dev/null 2>&1 ||
    die "Docker 没在运行。先 systemctl start docker，再重跑。"

say "拉取镜像"
if ! docker pull "$IMAGE"; then
    warn "从 ghcr.io 拉取失败，试试 Docker Hub 镜像"
    IMAGE="qninq/cdt-management:latest"
    docker pull "$IMAGE" || die "镜像拉取失败，检查网络或换用 docker compose 手动部署"
fi
LATEST="$(image_version)"

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    CUR="$(container_version)"
    RUNNING="$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || echo false)"
    if [ "${FORCE:-0}" != "1" ] && [ -n "$CUR" ] && [ "$CUR" = "$LATEST" ] && [ "$RUNNING" = "true" ]; then
        say "已安装，当前版本 ${CUR}，已是最新，容器不动（要强制重建加 FORCE=1）"
        [ -n "${URL_SHOWN:-}" ] || show_url
        exit 0
    fi
    say "已安装：当前 ${CUR:-未知} → 最新 ${LATEST:-未知}，开始升级"
    docker rm -f "$NAME" >/dev/null
fi

say "数据目录：$DIR"
mkdir -p "$DIR"

say "启动容器"
docker run -d --name "$NAME" --restart unless-stopped \
    --init \
    -p "${PORT}:8080" \
    -e TZ=Asia/Shanghai \
    -e CDT_DATA_DIR=/data \
    -v "${DIR}:/data" \
    "$IMAGE" >/dev/null

say "等待面板响应"
ready=0
for _ in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done

if [ "$ready" = 1 ]; then
    say "装好了，版本 ${LATEST:-未知}"
    show_url
    say "首次打开会引导设置管理员账号"
    say "重跑这条命令可以升级或卸载"
else
    say "容器已启动，但 40 秒内没等到响应。看日志：docker logs $NAME"
fi
