# The images CI runs as tools, pinned by digest so Dependabot keeps them current.
# Never built: `make lint-ci` and the workflows read each reference from its FROM
# line (`make tool-image NAME=zizmor`), so a bump here reaches both at once.
FROM ghcr.io/zizmorcore/zizmor:1.30.1@sha256:a2eb396d886c053073405c7a980f2139ba2248ec172243cfa3841e57196e8101 AS zizmor
FROM koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d AS shellcheck
