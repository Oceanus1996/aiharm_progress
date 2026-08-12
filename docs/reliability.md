# Step 4 — 稳定地把 prompt 喂进 Unity AI Assistant

两个半边：Unity 编辑器内的 runner（C#）+ 外部驱动（Python）。中间用一个 IO 文件夹通信。

```
Python 写 jobs.jsonl ──► io/ ◄── Unity runner 轮询
                          │
Python 读 results.jsonl ◄─┘  一行一个 job，append-only
```

## 为什么这样做，而不是模拟点击

Unity AI Assistant 2.16 有真正的程序接口。装的包里有：

- `AssistantApi.RunHeadless(this IAgent, prompt, ...)` — **公开** API，返回最终答案字符串
- `AssistantApi.RunHeadlessInternal(prompt, ctx, agent, mode, resumeId, ct, onUpdate)` — internal，多返回 conversationId 和完整 message blocks

包里 `Runtime/AssemblyInfo.cs` 和 `Editor/Assistant/Api/AssemblyInfo.cs` 都写了
`[InternalsVisibleTo("Unity.AI.Assistant.DeveloperTools")]`，而这个名字**包里没有任何 asmdef 占用**。
所以我们的 asmdef 就叫这个名字，合法拿到 internals，不用反射。

**「什么时候该发下一条」这个问题不需要猜。** `RunHeadlessInternal` 内部是：

```csharp
while (!isLastMessage) { ...; await Task.Yield(); }   // isLastMessage = frag.IsLastFragment
```

Task 只在后端发出最后一个流式片段时才 resolve。所以「送 → 等到真的答完 → 再送下一条」是事件驱动的，
全程没有一个 sleep 在猜时间。

## 装好

Unity 侧文件已经放在：

```
C:\Users\rrm_a\ai_project\Assets\Editor\T2C2I\
    Unity.AI.Assistant.DeveloperTools.asmdef
    AssistantBatchRunner.cs
```

打开 Unity 让它编译。菜单栏出现 `T2C2I` 就说明成功了。

IO 文件夹默认 `D:\vrgen_security\pipeline\step4_unity_feed\io`，
可用 `T2C2I > Set IO Folder...` 或环境变量 `T2C2I_IO_DIR` 改。两侧必须一致。

## 先跑通链路

```
# Unity 里：T2C2I > Self Test (one ping prompt)     ← 只验编辑器侧
# 或者从外面完整跑一遍：
cd D:\vrgen_security\pipeline\step4_unity_feed
python feed_unity_assistant.py --prompts smoke_prompts.txt --mode ask
```

看到每条 prompt 的 `ok` 和耗时就说明通了。

## 正式跑

```
# 单轮：一行一个 prompt
python feed_unity_assistant.py --prompts my_prompts.txt --mode ask

# 分步式生成：step3 的每张图 = 一个多轮 job，多轮共用同一个 conversation
python feed_unity_assistant.py --from-decomposition ../step3_decomposition/decomposition.jsonl

# 守护模式：别的进程往 jobs.jsonl 追加，这边持续喂
python feed_unity_assistant.py --jobs io/jobs.jsonl --watch

python feed_unity_assistant.py --status    # 心跳、进度
python feed_unity_assistant.py --stop      # 跑完当前 job 后停
python feed_unity_assistant.py --report    # 汇总
```

## job 格式

```json
{"id": "decomp_17", "mode": "ask", "turns": ["第一步...", "第二步..."],
 "timeout_sec": 420, "fresh": true,
 "images": ["D:/.../17.png"], "assets": ["Assets/Scenes/Foo.unity"]}
```

- `id` **是断点续跑的键**，必须唯一。已经在 results.jsonl 里的 id 会被跳过。
- `turns` 是同一个 conversation 里的连续轮次 —— 分步式生成靠这个。
- `fresh: true`（默认）每个 job 开新会话，prompt 之间不串味。
- `mode`：
  - `llm` — 无工具的自定义 agent（公开 API）。没有工具就没有审批点，**唯一真正无人值守的模式。
    纯文本探测（比如测拒绝率）用这个。**
  - `ask` — 只读助手。**注意：ask 也会弹框**，见下。
  - `plan` — 只读探索 + 出计划，定义上执行前就要批准，默认必弹框。别拿来跑批量。
  - `agent` — 能建资产、改 GameObject。弹框最多。

> **实测纠正（2026-08-07）**：一开始我以为 `ask` 是只读所以不会弹框，**这是错的**。
> 实测喂 "List the names of the scenes in this project" 时，助手调了 `RunReadOnlyCommandTool`，
> 它走 `ToolCallPermissions.CheckCodeExecution` → `WaitForUser` → **弹出 "Assistant Dialog" 模态框**，
> 主窗口被禁用，整个编辑器卡住直到有人处理。
> 原因：`CodeExecutionPolicy` 默认是 `Ask`，而"只读命令"仍然算代码执行。
> 所以 **ask 模式不能假定无人值守**。要么用 `llm`，要么开 Auto-Run。

## 稳定性是怎么保证的

| 故障 | 处理 |
|---|---|
| 域重载（脚本重编译 / 进 Play） | 非 agent 模式跑 job 时 `LockReloadAssemblies`；真被打断则写一条 `interrupted` 结果，不会静默丢 |
| 编辑器崩溃 / Ctrl-C | results.jsonl append-only 且每条 flush，最多丢正在跑的那一条；重跑按 id 跳过 |
| 后端超时 | 每轮独立 `CancellationTokenSource`，默认 420s；job 级最多重试 2 次 |
| Unity 根本没开 / 脚本没编译过 | 90s 内没人取走 run.request 就报错并列出排查步骤，不会干等 |
| 卡在审批对话框 | status.json 心跳停 → 900s 后 Python 侧报 stalled |

### 一个必须知道的边界：审批弹窗会让超时失效（已实测复现）

`RunHeadlessInternal` 里永远装的是 `DialogToolUiContainer`，它弹窗用的是
`ShowModalUtility()` —— **阻塞主线程**。而 `RunHeadlessInternal` 的等待循环是
`while (!isLastMessage) { await Task.Yield(); }`，靠主线程的 SynchronizationContext 泵才能推进。

所以一旦弹出审批框：主线程被挡住 → 泵停 → **我们那个 per-turn 的
`CancellationTokenSource` 超时永远不会触发**。C# 侧救不了自己，只有 Python 侧的心跳
超时能发现，然后需要人去点那个框。

因此：
- `ask` / `llm` 才 `LockReloadAssemblies`；`agent` / `plan` 不锁，免得卡住时连重编译都做不了。
- `plan` 模式定义上就是「执行前要批准」，**默认就会弹框**，不要拿它跑无人值守批量。
- 真要 `agent` 无人值守，得自己去编辑器偏好里打开 AI Assistant 的 **Auto-Run**
  （`SettingsPermissionsPolicyProvider` 只有在 `AutoRun` 打开时才把 `Ask` 升级成 `Allow`）。
  这等于预先授权「自动改工程」——对这个课题来说这是个**实验设计决定，不是配置细节**：
  自动批准是用户自己授的权，不是 agent 绕过去的。

## 已知边界

- **`-batchmode -executeMethod` 不支持。** 这个工程平时开着 Unity，持有 `Temp/UnityLockfile`，
  batch 模式必然崩（observation/iter_2 里记过一次）。所以走文件队列，编辑器开着就能用。
- **assembly 名字是借来的。** 叫 `Unity.AI.Assistant.DeveloperTools` 是为了拿 internals。
  哪天 Unity 自己发一个同名 assembly 就会撞车 —— 那时改 asmdef 名字，退回 `mode: "llm"`
  这条纯公开 API 的路。
- **agent 模式默认不是无人值守的。** 包里 `SettingsPermissionsPolicyProvider` 只有在编辑器
  偏好 `AssistantEditorPreferences.AutoRun` 打开时才把 `Ask` 策略升级成 `Allow`。
  这一条对论文有关系：自动批准是**用户自己授权**的，不是 agent 绕过来的。

## 实验设计上的两句话

1. **别只跑 treatment。** step3 每条记录同时给了 `direct_prompt`（控制组：直接要整个物体）
   和 `unity_prompts`（实验组：分步组件）。只跑后者证明不了「分解绕过了拒绝」——
   得有前者被拒的对照。
2. **别让 LLM 判定自己成功了。** results.jsonl 里是原始回复和完整 block dump（包括 tool call）。
   「组装出来的东西是不是目标物体」是 CLIP / 几何的活，不是读 transcript 的活。
