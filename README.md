# shhone-invoice-agent

구글시트 작업현황표를 감시해 단가를 산출하고 경리나라에 거래명세표를 발행하는 파이썬 에이전트.

## 폴더 구조

```
shhone-invoice-agent/
├── agent.py              # 진입점 (run / import / calibrate)
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
