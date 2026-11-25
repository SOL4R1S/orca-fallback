# Orca Fallback

[English](README.md) | **한국어**

감독형(supervised) Orca 워커를 위한 프로바이더 장애 조치(failover) 및 근거 기반 모델 선택 도구입니다.

이 프로젝트는 초기 공개 프로토타입입니다. OpenCodex(OCX)와 Antigravity에서 모델을 탐색하고, 사용자가 설정한 역할별 폴백 순서를 유지하며, Orca CLI를 통해 워커를 실행하고, 누락된 근거를 숨기지 않은 채 후보 모델의 순위를 산정합니다.

## 요구 사항

- Python 3.10+
- 감독형 워커 CLI를 지원하는 Orca 1.4.274 이상(또는 호환 버전)
- 모델 탐색을 위한 OCX 및/또는 Antigravity(`agy`)

## 설치 및 설정

```sh
git clone https://github.com/SOL4R1S/orca-fallback.git ~/orca-fallback
cd ~/orca-fallback
cp settings.example.json settings.json
cp selection.example.json selection.json
python3 -m unittest discover -s tests -v
python3 catalog.py refresh
python3 openrouter.py collect
```

저장소 외부의 설정 파일을 사용하려면 `ORCA_FALLBACK_CONFIG` 환경 변수를 지정하세요.
`settings.json`, `selection.json`, 생성된 카탈로그, 벤치마크, 인증 정보 및 런타임 측정 데이터는 의도적으로 버전 관리에서 제외되어 있습니다.

봇 및 자동화 복구 슈퍼바이저는 실험적 기능입니다. 실제 환경에서 실행하기 전에 [복구 검증 현황](docs/recovery-validation.md)을 먼저 확인하세요.

## 작업 실행

기존 워크트리(worktree)에서 해당 역할(role)에 선택된 모델들을 순서대로 사용해 Orca 작업을 실행합니다. 재시도는 실패한 워커가 종료(settled)되고 트랜스크립트에 프로바이더 가용성 오류가 포함된 경우에만 허용됩니다. 코드 오류나 테스트 실패가 발생하면 폴백 체인이 중단됩니다.

```sh
python3 ~/orca-fallback/run.py \
  --worktree /path/to/repository \
  --title 'Example task' \
  --spec-file /path/to/task.md
```

작업 명세 파일(`--spec-file`)에는 전체 요청 내용과 완료 기준(acceptance criteria)이 포함되어 있어야 합니다.
명령을 실행하면 최종 JSON 결과가 출력되며, 모든 실행 시도, 실제 사용된 모델, 디스패치 기록 및 오류 발췌문은 `~/.local/state/orca-fallback/`에 저장됩니다.

이 초기 버전은 감독형 워커를 처리할 뿐만 아니라, 워커·봇 채팅·예약 자동화(scheduled automations)를 위한 공용 디스패치 진입점(`dispatch.py`)과 제한적 복구 슈퍼바이저(`recovery.py`)도 포함하고 있습니다. 러너 또는 슈퍼바이저가 실행 중이거나 상태가 불확실한 대상과의 연결을 잃으면 PID를 강제 종료하거나 중복 실행 위험을 감수하는 대신 `blocked` 상태로 중지합니다.

## 공용 디스패치 및 제한적 복구

### 공용 디스패치 (`dispatch.py`)

봇과 자동화 작업은 프로바이더를 직접 실행하지 말고 `dispatch.py`를 호출해야 합니다. `dispatch.py`는 명시적인 `--role`과 `--lane`을 요구하며, 민감 정보가 마스킹(redacted)된 핸드오프/감사 기록을 `~/.local/state/orca-fallback/dispatches/` 아래에 저장한 뒤 `run.py`를 호출합니다.

```sh
python3 ~/orca-fallback/dispatch.py \
  --worktree /path/to/repository \
  --title 'Example task' \
  --spec-file /path/to/task.md \
  --role implementation \
  --lane economy \
  --caller-kind bot \
  --bot atlas \
  --chat-id chat_123
```

워커를 실제로 실행하지 않고 프로바이더 선택 결과를 확인하고 미리보기 기록을 저장하려면 `--dry-run` 옵션을 전달하세요.

실행 모드의 `dispatch.py` 호출이 `lane_exhausted`를 반환하면(모델들이 프로바이더 오류로 실패했거나 레인 내 모든 모델이 쿨다운 상태여서 건너뛴 경우 모두 포함), `~/.local/state/orca-fallback/recoveries/`에 지속성 있는 복구 레코드를 자동으로 등록합니다. 이 레코드에는 `supervision_schema_version: 2`, `unattended_supervision` 옵트인 플래그(기본값 `true`, 비활성화하려면 `--no-unattended` 전달), 보존된 `excluded_providers`(`--skip-provider`), 그리고 실제로 시도된 모델만 담긴 `attempted_providers`가 기록됩니다. 쿨다운으로 건너뛴 모델은 쿨다운 만료 후 상태 점검(health probe)을 거친 지연 재개(delayed resume) 대상 자격을 유지합니다. 제외되지 않은 레인의 모든 후보 모델이 작업이나 부수 효과(side effects) 발생 이전에 발생한 것으로 입증된 종료 상태의 레이트 리미트/일시적 프로바이더 실패를 겪은 경우, 동일 레인에서 정확히 `OK`를 반환하는 상태 점검 이후 최대 1회의 쿨다운 후 재시도가 허용됩니다. 인증 오류, `model_not_found`, 그리고 도구나 워크트리에 부수 효과를 남긴 작업은 절대 자동 재시도되지 않으며 명시적인 `blocked` 상태로 전환됩니다.

### 범위 지정 디스패치 연동 (`wiring.py` 및 `mcp_dispatch.py`)

`wiring.py`는 기본적으로 읽기 전용 드라이런(dry-run) 모드로 동작하며(실제 변경을 적용하려면 `--execute` 필요), 지원되는 Orca CLI 인터페이스를 통해 **위임된 작업(delegated tasks)**을 연동합니다.

- **봇 위임 연동 (`install-bot` / `uninstall-bot`)**: 순수 표준 라이브러리 기반 MCP stdio 서버(`mcp_dispatch.py`, `orca_fallback_dispatch` 및 `orca_fallback_status` 제공)를 `orca-ide bot tools add`를 통해 봇별로 등록하고, 해당 봇의 `SOUL.md`에 마커로 구분된 짧은 안내 블록을 추가합니다(Orca가 관리하는 `AGENTS.md`, `CLAUDE.md`, `bot.json`은 수정하지 않습니다).
- **적격 예약 자동화 연동 (`install-automation` / `uninstall-automation`)**: 안전성 검사(비-`once`, 존재하는 로컬 워크트리, 비-`new-per-run`, 비-`reuseSession`, 비-복구용)를 통과한 반복 예약 자동화만 명시적으로 등록합니다. 등록 이전 실행이 재실행되지 않도록 `enrolled_at`과 `baseline_run_ids`를 기록하며, `orca-ide automations edit`를 통해 자동화 프롬프트에 마커로 구분되어 되돌릴 수 있는 디스패치 지침을 감싸 추가합니다. 레거시 자동화, 일회성 자동화, 미등록 자동화는 기본적으로 건너뜁니다.
- **결정적 로컬 예약 디스패치 (`install-scheduled-dispatch` / `uninstall-scheduled-dispatch`)**: 외부 LLM 프롬프트를 거치지 않고 로컬 워크트리에서 `dispatch.py`를 직접 호출하는 옵트인 방식의 기본 드라이런 systemd 사용자 `.service`/`.timer` 쌍(`orca-dispatch-<name>.*`)을 설치합니다.

**네이티브 Orca 경계**: 네이티브 Orca 봇 채팅 턴과 Orca 예약 자동화는 항상 내부적으로 LLM 에이전트(`--provider` 또는 `--prompt`와 함께 `--bot`)를 시작하므로, Orca가 프로바이더를 호출하기 전에 외부에서 가로챌 수 없습니다(Orca에는 명령어 전용 자동화 모드가 없고, `--precheck`는 단순 스케줄 게이트일 뿐이며, 헤드리스 `orca-serve`에는 플러그인 훅 서비스가 없습니다). `SOUL.md`와 프롬프트 블록은 위임된 작업을 위한 에이전트 안내 지침일 뿐 강제 인터포저(interposer)가 아닙니다. 결정적인 로컬 워크트리 스케줄링에는 `install-scheduled-dispatch`를 사용하고, 종료된 프로바이더 장애에 대한 사후 처리는 `recovery.py`가 담당합니다.

### 복구 슈퍼바이저 (`recovery.py`) 및 systemd 타이머

`recovery.py`는 기본적으로 읽기 전용 드라이런 모드로 동작합니다. 실제 변경을 수행하려면 명시적으로 `--execute` 플래그를 전달해야 합니다.

```sh
# Dry-run preview (default)
python3 ~/orca-fallback/recovery.py recover \
  --kind bot_chat --bot atlas --chat-id chat_123 \
  --role implementation --lane premium --failed-provider codex:gpt-6.2-sol

# Live execution (creates a disabled one-time recovery automation and triggers it once -> waiting_for_run)
python3 ~/orca-fallback/recovery.py recover \
  --kind bot_chat --bot atlas --chat-id chat_123 \
  --role implementation --lane premium --failed-provider codex:gpt-6.2-sol --execute

# Reconcile an accepted recovery run against terminal status before marking recovered
python3 ~/orca-fallback/recovery.py reconcile --recovery-id <id> --execute

# Run one bounded reconcile + delayed-resume supervisor pass (dry-run by default)
python3 ~/orca-fallback/recovery.py supervise --execute

# Install and enable the oneshot supervisor timer (`units/orca-recovery-supervisor.*`)
python3 ~/orca-fallback/wiring.py install-systemd --enable --execute

# Run the isolated, loopback-only live E2E harness (creates disposable temp ORCA_USER_DATA_PATH)
python3 ~/orca-fallback/scripts/e2e_isolated.py
```

핵심 안전 보장 사항:

- **멱등성 및 파일 잠금**: 모든 실제 복구, 조정(reconciliation), 지연 재개 작업은 `~/.local/state/orca-fallback/recoveries/by_key/` 아래에서 키별 `flock` 잠금을 획득하며, 실행이 `waiting_for_run` 상태이거나 이미 완료된 경우 중복 실행 없이 기존 결과를 반환합니다.
- **종료 확인 후 복구 및 PID 강제 종료 금지**: 워커는 `worker-show`에서 종료된 실패 상태여야 하며, 봇 채팅은 `bot chats` 및 `agent show`, `agent read`(`orca_session_id:<chat_id>`)를 통해 `state=done`임이 입증되어야 합니다. 실행 중이거나 상태가 불확실한 대상은 아무런 변경 없이 `blocked`(`active_or_uncertain`)를 반환합니다. Orca는 감독형 디스패치용 `worker-stop`은 제공하지만 범용 에이전트 중지나 자동화 취소 명령은 제공하지 않으므로 임의의 중지 API를 가정하거나 만들어내지 않습니다.
- **프로바이더 오류 전용 폴백**: 테스트 실패, 린트 실패, 일반적인 구현 오류 또는 사람의 승인/입력 대기(`observation.agentWait`)에서는 절대 폴백하지 않습니다. 역할/레인 내의 각 프로바이더는 한 번씩만 시도되며, 순환 재시도하거나 상위 레인으로 자동 에스컬레이션하지 않습니다.
- **중복 실행 없는 신규 봇 채팅 시작**: 크로스 프로바이더 봇 채팅 복구는 `~/.orca/bots/*/bot.json`을 통해 봇 표시 이름을 확인하고, `SOUL.md`, `MEMORY.md`, `items`를 보존하며, `--disabled` 상태의 일회성 봇 자동화(`--runs-in new --once --by ...`)를 생성한 뒤 `automations run`으로 단 한 번 트리거합니다(`reconcile`이 최종 성공 상태를 검증할 때까지 `waiting_for_run` 유지).
- **예약 자동화 선행 일시 중지**: 복구를 실행하기 전에 기존 스케줄을 먼저 비활성화합니다. 기본적으로 비활성화 상태를 유지하며, `settings.json`의 `recovery.auto_resume_automation`이 `true`이고 복구 실행이 최종 `succeeded` 상태로 조정된 경우에만 다시 활성화합니다.
- **다음 작업부터 기본 프로바이더 복귀 및 상태 검증 후 지연 재개**: 현재 실행 중인 폴백 작업은 도중에 프로바이더를 전환하지 않으며, 할당량(quota) 초기화 후 기본 프로바이더로의 복귀는 다음 작업부터 적용됩니다. 레인 내 모든 프로바이더를 사용할 수 없는 경우 `delayed_resume_plan`이 저장됩니다. `recovery.py resume`은 재개 시에도 명시적인 `excluded_providers`를 유지하고, 아직 시도되지 않은 적격 프로바이더를 우선 고려하며(또는 제외되지 않은 모든 후보가 작업·부수 효과 발생 전에 종료된 레이트 리미트/일시적 실패를 겪은 것이 입증된 경우 최대 1회의 쿨다운 후 재시도만 허용), 인증 오류·`model_not_found`·부수 효과가 발생한 실행은 절대 자동 재시도하지 않으며, 작업 시작 전 동일 레인에서 제한적 읽기 전용 추론 프로브(`codex exec -s read-only` 또는 `agy --print --sandbox`)가 정확히 `OK`를 반환해야 합니다.
- **옵트인 무인 감독 및 타이머 간격 제어**: `recovery.py supervise`는 `supervision_schema_version: 2`와 `unattended_supervision: true`를 포함하는 새로 기록된 레코드만 처리하며(동일 형식의 과거 이력 레코드 및 `--no-unattended` 레코드는 건너뜀), 등록 시점 이전에 기록된 등록 자동화 실행도 건너뜁니다. `units/orca-recovery-supervisor.timer`는 긴 슈퍼바이저 실행 때문에 다음 주기가 유실되지 않도록 `OnUnitInactiveSec=1min`을 사용합니다.
- **민감 정보 마스킹**: API 키, Bearer 토큰, Slack/GitHub/Linear 토큰, Orca 페어링 코드는 저장되는 상태 파일과 영수증에서 마스킹 처리됩니다.

### 운영 로그

실행 모드의 디스패치와 복구는 타임스탬프가 포함된 텍스트 로그를 `~/.local/state/orca-fallback/operations.txt`에 기록합니다. `tail -F ~/.local/state/orca-fallback/operations.txt`로 실시간 확인할 수 있습니다. 각 줄에는 핸드오프 또는 복구 ID, 상태, 관련 모델 또는 실패 분류가 기록됩니다. 프롬프트, 작업 제목, 원시 모델 출력은 이 로그에 남기지 않으며, 상세 기록은 해당 ID를 이용해 `dispatches/` 또는 `recoveries/` 디렉터리에서 확인할 수 있습니다. 로그는 1 MiB 단위로 순환(rotate)되며 이전 로그 파일 1개(`operations.previous.txt`)를 보관합니다. 두 파일 모두 `ORCA_FALLBACK_STATE_DIR`이 설정되어 있으면 해당 경로를 따릅니다.

## 모델 탐색 및 선택

`catalog.py`는 OCX의 실시간 `/v1/models` 카탈로그와 `agy models`를 읽어옵니다. 어느 한쪽 시스템에 새로 등록된 모델은 갱신(refresh) 후 목록에 나타납니다. 갱신은 사용자의 역할별 모델 선택을 덮어쓰거나, 사라진 모델을 임의로 다른 모델로 대체하지 않습니다. 실시간 탐색에 실패한 OCX 설정 모델은 일시적인 인증 또는 할당량 오류로 인해 지워지지 않도록 `unavailable` 상태로 계속 표시됩니다.

```sh
python3 ~/orca-fallback/catalog.py refresh
python3 ~/orca-fallback/catalog.py list --role implementation
python3 ~/orca-fallback/catalog.py select --role implementation \
  --models codex:gpt-6.2-sol=high \
           antigravity:gemini-4-hydrogen-high \
           codex:command-code/z-ai/glm-5.3-flash=low
```

역할은 `orchestrator`, `implementation`, `review`, `documentation`, `research` 다섯 가지입니다. 사용자 선택은 `selection.json`에, 생성된 모델 인벤토리는 `catalog.json`에 저장됩니다. 선택된 모델이 카탈로그에서 사라지면 사용자가 대체 모델을 선택할 때까지 다음 실행이 차단됩니다.

### 비용 및 품질 레인

각 역할에는 독립적인 폴백 순서, 절대 비교 가격 상한선, 최소 지능 점수 기준을 가진 `premium` 레인과 `economy` 레인이 있습니다. `economy` 레인은 자동으로 `premium`으로 에스컬레이션되지 않습니다. 적격한 모든 모델이 소진되면 `lane_exhausted`를 반환하며, 더 비싼 시도를 승인하려면 사용자가 명시적으로 `--lane premium`으로 다시 실행해야 합니다.

```sh
python3 run.py --worktree /path/to/repo --title 'Summarize sources' \
  --spec-file /path/to/task.md --role research --lane economy
python3 catalog.py list --role research --lane economy
python3 catalog.py select --role research --lane economy \
  --models codex:gpt-6-luna=low \
           codex:command-code/z-ai/glm-5.3-flash=low \
           codex:alibaba-token-plan-sg/deepseek-v4.1-flash-1017=low
```

가격이나 품질 근거가 누락되어 있다고 해서 이 제한을 우회할 수는 없습니다. 기존 설정의 레거시 역할 배열 형식도 계속 지원됩니다.

포함된 `units/orca-model-catalog.timer`는 6시간마다 이 인벤토리를 갱신합니다. 이때 `catalog.json`만 업데이트하며, 사용자의 선택 목록은 절대 건드리지 않습니다.

## OpenRouter 근거 수집

`openrouter.py`는 공개 `/api/v1/models` JSON을 직접 읽어 OpenRouter 가격 정보, Artificial Analysis 지수, Design Arena 점수를 수집합니다. 정확한 ID와 검증된 별칭(alias)만 보수적으로 매칭하며, 유사한 이름이나 다른 버전은 동일 모델로 취급하지 않습니다. 원시 JSON은 `~/.cache/orca-fallback/`에 캐시되며, 정규화된 항목에는 출처 URL, 수집 시각, 소스 해시가 함께 유지됩니다.

OpenRouter 가격은 OpenRouter 종량제 사용 기준을 나타내며, OCX 구독 및 토큰 플랜 과금은 다를 수 있으므로 두 값은 별도로 관리됩니다.

## 근거 기반 추천

`recommend.py`는 출처가 확인된 OCX 또는 OpenRouter 가격 메타데이터, 로컬 성공률/지연 시간 관측치, 선택적 공개 벤치마크 기록을 바탕으로 각 봇 역할에 사용 가능한 모델의 순위를 산정합니다. 누락된 가격이나 벤치마크 점수를 임의로 추론하지 않습니다. 공개 벤치마크 항목은 `benchmarks.json`에 정확한 모델 키, 벤치마크 이름, 점수, 출처 URL, 검증 날짜가 포함되어 있어야 합니다. 서로 다른 벤치마크의 원시 점수는 비교 전에 각각 정규화됩니다.

```sh
python3 ~/orca-fallback/recommend.py --role implementation
python3 ~/orca-fallback/recommend.py --role research --format json
python3 ~/orca-fallback/recommend.py --role documentation --ai
```

기본 동작은 읽기 전용입니다. `--ai` 옵션은 선택된 `orchestrator` 모델에게 도구 없이 제공된 근거만을 바탕으로 설명을 생성하도록 요청합니다. `--apply`는 화면에 표시된 상위 3개 후보로 해당 역할의 폴백 체인을 교체하는 명시적 사용자 동작입니다. 로컬 실제 작업 측정 기록은 `~/.local/state/orca-fallback/performance.jsonl`에 추가됩니다.

추천 엔진은 결정적(deterministic)으로 동작하며, AI 설명은 선택 사항입니다. 결과는 동일한 기반 모델에 대한 경로(route)들을 그룹화하고, 대체 프로바이더를 표시하며, 봇 역할에 따라 벤치마크 가중치를 적용합니다. 공개 리더보드 순위를 운영 환경 추천으로 취급하기 전에 로컬 작업 측정 근거가 여전히 필요합니다.

## 쿨다운

프로바이더 장애가 발생하면 LiteLLM과 Portkey에서 영감을 받은 설정 가능한 쿨다운이 발동합니다. 이후 실행되는 작업들은 쿨다운이 만료될 때까지 해당 모델을 건너뛰며, 호출이 성공하면 쿨다운이 해제됩니다. 작업 자체의 실패나 테스트 실패는 절대 프로바이더 폴백을 유발하지 않습니다.

## 설계 참고 사항

- LiteLLM과 Portkey에서 명시적 재시도/폴백 정책 및 프로바이더 장애 처리 방식을 참고했습니다.
- OpenRouter의 공개 모델 API가 추가 가격 및 벤치마크 근거를 제공합니다.
- 워커 수명 주기, 터미널, 워크트리, 작업 상태에 대한 단일 진실 공급원(source of truth)은 여전히 Orca입니다.

크로스 프로바이더 봇 채팅 복구는 일회성 봇 자동화(`--runs-in new`)를 통해 **새로운** 봇 채팅을 시작하며, 기존 채팅에서 프로바이더를 제자리 교체할 수 있다고 주장하지 않습니다. 또한 이 프로젝트는 공개 벤치마크 점수가 사용자의 비공개 워크로드에서의 성공을 보장한다고 주장하지 않습니다.

초기 측정 결과와 현재 제약 사항은 [재사용 평가 결과](docs/reuse-evaluation.md)를 참고하세요.
