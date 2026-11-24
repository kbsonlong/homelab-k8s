# 由主 Makefile include
.PHONY: waf-obs-verify waf-plan waf-apply-routes waf-verify waf-verify-block waf-mirror-off waf-mirror-on waf-mirror-status waf-secrets waf-unblock

waf-plan:            ## 只读：渲染 + ID 冲突检查 + diff
	@scripts/waf-plan.sh

waf-apply-routes:    ## 应用加了 RequestMirror 的业务路由（argocd/gitea；grafana 由 Argo CD 同步）
	@scripts/render.sh
	kubectl apply --server-side --force-conflicts -f rendered/gateway-api.yaml

waf-verify:          ## P0 验收：主链路 + 日志
	@scripts/waf-verify.sh

waf-verify-block:    ## Gateway（Envoy）层 CF-Connecting-IP 封禁验证（用临时测试路由，不改真实名单）
	@scripts/waf-verify-gateway-block.sh

waf-mirror-off:      ## 紧急关闭所有镜像
	@scripts/waf-mirror.sh off
waf-mirror-on:
	@scripts/waf-mirror.sh on
waf-mirror-status:
	@scripts/waf-mirror.sh status

waf-secrets:         ## 从 .env 生成 waf-console 的 Secret
	@scripts/waf-secrets.sh

waf-unblock:         ## make waf-unblock IP=1.2.3.4（或 IP=--all 清空）
	@test -n "$(IP)" || (echo "IP=? required"; exit 2)
	@scripts/waf-unblock.sh "$(IP)"

waf-obs-verify:      ## 可观测性链路：字段、LogsQL、告警、看板
	@scripts/waf-observability-verify.sh
