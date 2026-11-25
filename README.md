# shhone-invoice-agent

구글시트 작업현황표를 감시해 단가를 산출하고 경리나라에 거래명세표를 발행하는 파이썬 에이전트.

## 폴더 구조

```
shhone-invoice-agent/
├── agent.py              # 진입점 (run / import / blocks / aliases / clients / calibrate)
├── clients_cli.py        # 거래처 별칭표 검사·후보 제안·마스터 검색
├── watcher.py            # 구글시트 감시
├── pricer.py             # 단가 산출
├── notifier.py           # SMS 알림
├── feedback.py           # 경리나라 내려받기 → SQLite 적재
├── issuer/
│   ├── excel_upload.py   # 엑셀 업로드
│   ├── actions.py        # 화면 조작
│   └── vision.py         # 화면 캡처/비전 검증
├── config/
│   ├── settings.py       # .env 설정 로더
│   ├── sheets.yaml       # 시트 구조 설정
│   ├── client_aliases.yaml  # 거래처 별칭표 (시트 표기 → 정식 등록명/사업자번호)
│   └── pricing_rules.md  # 단가 규칙
├── data/
│   ├── agent.db          # SQLite (git 제외)
│   ├── import/           # 경리나라 내려받기 파일
│   └── screenshots/      # 화면 캡처
└── tests/
```

## 실행법

```bash
pip install -r requirements.txt
cp .env.example .env      # 값 채우기
python agent.py run
```

## 거래처 별칭

시트 표기가 경리나라 등록명과 달라 `보류-미등록`이 되면 `config/client_aliases.yaml`에 별칭을 적는다
(watcher가 매 폴링 다시 읽음).

```bash
python agent.py clients find 에스티엠   # 정식명 / 사업자번호 / 최근거래 / 올해 건수
python agent.py aliases suggest         # 두 시트의 미해석 거래처 → 후보 (yaml 주석 형식)
python agent.py aliases check           # 별칭표 검사 (없는 등록명·번호 형식·약칭 중복, homonyms 번호)
python agent.py clients add "QMESYS(큐머시스)" 108-12-95838   # import에 없는 거래처 수동 등록
```
