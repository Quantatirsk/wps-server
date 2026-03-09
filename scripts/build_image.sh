#!/usr/bin/env bash
set -euo pipefail

default_image_name="wps-api-service"
default_image_tag="latest"

read -r -p "镜像名 [${default_image_name}]: " image_name
image_name="${image_name:-$default_image_name}"

read -r -p "镜像标签 [${default_image_tag}]: " image_tag
image_tag="${image_tag:-$default_image_tag}"

read -r -p "是否使用 --no-cache 构建? [y/N]: " no_cache_answer
no_cache_answer="${no_cache_answer:-N}"

read -r -p "是否覆盖 WPS_DEB_URL_BASE? 留空则使用 Dockerfile 默认值: " wps_deb_url_base
read -r -p "是否覆盖 FONTS_ZIP_URL? 留空则使用 Dockerfile 默认值: " fonts_zip_url

build_cmd=(docker build -t "${image_name}:${image_tag}")

case "${no_cache_answer}" in
  y|Y|yes|YES)
    build_cmd+=(--no-cache)
    ;;
esac

if [[ -n "${wps_deb_url_base}" ]]; then
  build_cmd+=(--build-arg "WPS_DEB_URL_BASE=${wps_deb_url_base}")
fi

if [[ -n "${fonts_zip_url}" ]]; then
  build_cmd+=(--build-arg "FONTS_ZIP_URL=${fonts_zip_url}")
fi

build_cmd+=(.)

echo
echo "将执行以下命令:"
printf '  %q' "${build_cmd[@]}"
echo

read -r -p "确认开始构建? [Y/n]: " confirm_answer
confirm_answer="${confirm_answer:-Y}"

case "${confirm_answer}" in
  n|N|no|NO)
    echo "已取消。"
    exit 0
    ;;
esac

"${build_cmd[@]}"
