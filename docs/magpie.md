# 接 magpie 通道

[magpie](https://github.com/yetone/magpie) 是一个本地模型网关（MIT）：它在
`127.0.0.1:3425` 起一个服务，同时说 OpenAI Chat / OpenAI Responses /
Anthropic Messages / Gemini 四种协议，并在它们之间互转。

接到荣珂之后，「模型从哪来」变成一处可切换的选择，而不是手改三个字段。

## 最省事的一条路：设置页点一下

打开荣珂 → 设置 → **「用 magpie 的模型」那张卡**：

1. 卡片自动问本机 gateway 在不在听，并把**网关上有哪些模型**列出来；
2. 点一个模型 → 切过去。`base_url`、`api_key_env`、`model` 三个字段
   **一个都不用填**；
3. 密钥也顺手存好了 —— magpie 网关接受任意 key，荣珂在切换时自动把
   `MAGPIE_API_KEY` 存成 `magpie`（存进你家目录的 `~/.rongke/secrets.json`，
   不在项目里）。

网关没起的时候，卡片不打哑谜：它列出启动方式（桌面版 / Docker 命令），
并明确说「不想用它也没关系，下面那些字段照样可以手填」。

网关在**别的机器**上（Docker 装在内网服务器）：点卡片上的「连别的机器…」，
填那台的地址即可。设置页会记住你填的地址，直到关掉页面。

## 命令行也行

```bash
rongke --magpie                            # 只体检：网关在不在听，不改配置
rongke --magpie deepseek/deepseek-v4-pro   # 切过去
```

第二条会写配置并打印下一步。模型名照抄 magpie 里的写法（面板里能看到，
或跑 `magpie quota`）—— 荣珂不校验它，因为模型清单归 magpie 管。

## 还差一步：让密钥变量非空

设置页那条路**不用管这一步**（已经替你存好了）。走命令行时：

```powershell
$env:MAGPIE_API_KEY="magpie"      # PowerShell
```

```bash
export MAGPIE_API_KEY=magpie      # bash / zsh
```

---

> 以下为开发者信息。使用者只需要上面那几步。

## 客户端形态：Docker / 内网服务器

客户机器上不必装桌面版。magpie 有官方镜像：

```bash
docker run -d --name magpie \
  -p 127.0.0.1:3425:3425 \
  -v magpie-config:/config \
  ghcr.io/yetone/magpie
```

网关在别的机器上时：

```bash
rongke --magpie deepseek/deepseek-v4-pro --magpie-host http://192.168.1.9:3425
```

⚠️ 官方文档明确要求：**不要把 3425 直接暴露在公网**。放在防火墙或 VPN 后面。
Docker 形态记得 `-p` 只绑 loopback，除非确实需要内网其他机器访问。

## 走 magpie 时会关掉什么

`send_reasoning_effort` 会被写成 `false`。

原因：荣珂默认通道（commandcode）**必须**带 `reasoning_effort`，不带就静默
不思考、不报错、给出看起来正常的错答案。换成 magpie 之后，上游要什么由
magpie 决定，荣珂不再强加。

## 自动补密钥的三个前提

`server.py` 的 `settings_save` 里那段只在**同时**满足这三条时才动密钥：

1. 目标 `base_url` 确实是 magpie（`is_magpie_base`，按端口 3425 判定）；
2. 当前没有任何可用密钥（有就用用户的，绝不覆盖）；
3. 环境变量里也没设（那种情况已经能用了，别多事）。

任一不满足就不碰密钥 —— 否则会出现「切到别家服务商时被 magpie 的占位值
顶替了 key」这种串台。`magpie_key_test.py` 有一半是这三条的负控。

## 换回去

```bash
rongke --setup          # 看当前 provider 状态
```

或者直接在 `config.json` 里改回原来的 `base_url` / `api_key_env` / `model`。
每次写配置前都会备份成 `config.json.bak_<时间戳>-<短随机>`。

## 已验证 / 未验证

**已验证**（`magpie_test.py`、`magpie_switch_test.py`、`magpie_ui_test.py`、
`magpie_key_test.py`）：

- 地址拼装：默认 loopback、Docker 形态、缺端口补 3425、不叠成 `/v1/v1`
- 鉴权头与协议：荣珂真发一次 `/v1/chat/completions`，拿到回复并正确回填用量
- `probe()` 的**正负控**：没人监听时说连不上；有服务时说在线
- 写配置：三个键写对、`send_reasoning_effort` 关掉、盘上无密钥、
  其它段不丢、写前备份、白名单外的键**必须抛错**
- 设置页卡片：真浏览器里打开设置页 → 卡片出现 → 网关不在线时给启动指引 →
  网关在线时模型列成可点按钮 → 页面无 JS 报错
- 自动补密钥：走真 HTTP 接口验证「切 magpie 补上 / 切别家不碰」

**未验证**：真实 magpie 网关上的端到端。

这次开发机上没有 Go、没有 Docker、到 GitHub release CDN 也不通，
所以上面的端到端是用一个**只实现 `/v1/models` 与 `/v1/chat/completions`、
其余路径回 404** 的本地假网关做的。它证明荣珂这一侧的契约是对的，
**不能**证明真 magpie 的转发、多渠道失败切换、订阅账号行为也对。

第一次真接上 magpie 时，请确认：

1. 设置页那张卡显示「网关在线」，并列出模型
2. 点一个模型 → 显示「已切到 magpie」，且顶部不再喊「还没有密钥」
3. `rongke --setup` → provider 诊断应是 OK
4. `rongke "写下我课题的下一步"` → 真跑一次，看有没有真回话

第 3 步不过，多半是上游 key 没在 magpie 里配（那是 magpie 的事，
不是荣珂的）；第 4 步报 4xx，先看 `MAGPIE_DEBUG=1` 起 magpie 打出来的日志。
