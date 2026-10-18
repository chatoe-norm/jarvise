# Context: Jarvise EV-Optimization Engine & Doctrine Guardrail Upgrade

> **Superseded (2026-10-18).** Kept only as the original prompt for the record. Its Prisma / Next.js / Server Actions architecture is not Jarvise and is forbidden by `.cursor/rules/jarvise-trading-rules.mdc`. The aligned version, with what exists and what is still a candidate, is [jarvise-opus55-extra-high.md](jarvise-opus55-extra-high.md). Research overlay only: do not index into Qdrant `jarvise_doctrine` and do not use as an implementation prompt.

## 1. Objective

พัฒนาและอัปเกรดเครื่องยนต์คำนวณความได้เปรียบเชิงสถิติ (**Expected Value - EV Optimization Engine**) และชั้นคัดกรองความเสี่ยง (**Doctrine Guardrail Layer**) สำหรับโปรเจกต์ **Jarvise Autonomous Trader** บน **Antigravity IDE** เพื่อให้ทุกสัญญาณที่เข้าสู่ตาราง `Approval Queue` มีค่า $EV$ สุทธิเป็นบวกหลังหักต้นทุนจริง ผ่านการถ่วงน้ำหนักข้อมูลหลายมิติ (Price Structure, Volume/VWAP, On-Chain/Derivatives, และ Macro Context) และเคารพกฎการรักษาเงินต้น (Capital Preservation) อย่างเคร่งครัด

## 2. Strict Architectural & Coding Rules for Claude

- **Layered Architecture Enforcement:**
  - บังคับใช้โครงสร้าง `Server Actions / Controllers` -> `Services` -> `Repositories` อย่างเคร่งครัด
  - ห้ามเรียกใช้ Prisma / SQLite โดยตรงจาก Server Components หรือ Actions เด็ดขาด ต้องผ่าน Repository เท่านั้น
  - ทุกเมธอดใน Repository ต้องห่อหุ้มด้วย `handlePrismaError` (หรือ Database Error Handler มาตรฐานของโปรเจกต์)
- **Precision Arithmetic:**
  - ห้ามใช้ `number` (Float) มาตรฐานของ JavaScript ในการคำนวณราคา ขนาดออเดอร์ ค่าธรรมเนียม หรือสมการ $EV$ โดยตรง ให้รับค่าเป็น `string` และแปลงผ่านคลาส `Decimal` (`decimal.js`) ก่อนคำนวณทุกครั้ง
- **UI Synchronization:**
  - ทุกครั้งที่ Server Action มีการอัปเดตสถานะสัญญาณหรือสถานะ Kill-Switch ต้องเรียกใช้ `revalidatePath` และซิงค์กับ `router.refresh()` เสมอ

## 3. Mathematical & Quantitative Specifications

### 3.1 Net Expectancy ($EV$) Gatekeeper

ทุกกลยุทธ์และสัญญาณต้องพิสูจน์ว่ามีค่าคาดหวังสุทธิเป็นบวก ($EV > 0$) ก่อนอนุญาตให้เข้าสู่โหมดใช้งานจริง:

$$EV_{\text{net}} = (\text{Win}\% \times \text{AvgWin}) - (\text{Loss}\% \times \text{AvgLoss}) - (\text{ExchangeFee} + \text{SpreadCost} + \text{ExpectedSlippage})$$

- **Local Venue Cost Realism:** คำนวณค่าธรรมเนียมจริง ส่วนต่างราคา Bid-Ask และความลึกของ Order Book ตามเงื่อนไขของกระดานเทรดจริง (เช่น Binance TH)
- หาก $EV_{\text{net}} \le 0$ ระบบต้องปัดตกสัญญาณและบังคับคืนค่าสถานะเป็น `FLAT` ทันที

### 3.2 Position Sizing & Volatility Stop-Loss Rules

- **The Zero-Compromise 1% Rule:** ความเสี่ยงสูงสุดต่อไม้ห้ามเกิน `1.0%` ของมูลค่าพอร์ตทั้งหมด (ช่วงเริ่มต้นแนะนำที่ `0.25% - 1.0%`)
- **Fractional Kelly Ceiling:** ใช้สูตร Kelly Criterion เป็นเพียงเพดานสูงสุดทางคณิตศาสตร์เท่านั้น ห้ามใช้ค่าเต็ม (Full Kelly) ให้คูณลดทอนเป็นเศษส่วน (Fractional Kelly เช่น `0.25x - 0.50x`) และลดขนาดไม้ลงอีกเมื่อสภาวะตลาดไม่ชัดเจน
- **ATR Invalidation Distance:** ทุกสัญญาณต้องกำหนดจุด Stop-Loss (Invalidation Level) ที่ชัดเจนก่อนเข้าเทรด โดยระยะห่างของ Stop-Loss ต้องไม่แคบกว่า `1.5` เท่าของค่าความผันผวนปัจจุบัน ($\text{Stop Distance} \ge 1.5 \times \text{ATR}$) เพื่อป้องกันการถูกสะบัดกิน Stop จาก Noise ปกติ

## 4. Multi-Layer Analyzer Stack & Believability Scoring

ให้สร้าง `BelievabilityScorerService` โดยถ่วงน้ำหนักคะแนนความมั่นใจ (`0.00` ถึง `1.00`) ตามแนวคิด Idea Meritocracy (Ray Dalio) จาก 3 ชั้นวิเคราะห์ดังนี้:

### 4.1 Layer 1: Price-Graph & Microstructure Analyzer (Weight: 50%)

- **Structure First:** จำแนกสภาวะตลาด (`Regime`) ออกเป็น `TREND`, `RANGE`, หรือ `CHAOTIC` พร้อมระบุจุด Swing High/Low, แนวรับ-แนวต้าน และรูปแบบ Break-and-Retest
- **Candle & Trend Filtering:** ใช้กราฟ Heikin-Ashi ช่วยกรองสัญญาณรบกวนของแนวโน้ม ใช้แท่งเทียนกลับตัว (Engulfing / Doji) เป็นเพียงบริบทประกอบ และวัดความแข็งแกร่งของเทรนด์ด้วยตระกูล EMA/SMA, ADX, และ Parabolic SAR
- **Volatility Squeeze vs. Expansion:** ตรวจจับการบีบตัวและระเบิดตัวของความผันผวนด้วย Bollinger Bands, Keltner Channels, และ Donchian Channels
- **Momentum Divergence Only:** ใช้ RSI, MACD, Stochastic Oscillator, MFI และ CCI เพื่อตรวจหาสัญญาณขัดแย้งกับราคา (Divergence) เท่านั้น ห้ามใช้ค่า Oscillator เดี่ยวๆ เป็นตัวสั่งเปิดออเดอร์เด็ดขาด
- **Volume & Fair Value Confirmation:** ยืนยันการเบรกเอาต์ด้วย On-Balance Volume (OBV), Accumulation/Distribution (A/D), Chaikin Money Flow (CMF), Volume-at-Price (POC) และใช้เส้น Session VWAP เป็นจุดอ้างอิงราคายุติธรรมระหว่างวัน (การเบรกเอาต์ที่วอลุ่มบางให้ตัดคะแนนความมั่นใจลงทันที)

### 4.2 Layer 2: On-Chain & Derivatives Analyzer (Weight: 30%)

- **On-Chain Flows:** ตรวจสอบทิศทางเงินทุนไหลเข้ากระดาน (Exchange Inflows = แรงเทขาย/สัญญาณลบ) และไหลออกจากกระดาน (Exchange Outflows = การสะสม/สัญญาณบวก) รวมถึงระดับ Exchange Reserves
- **Derivatives Stress (CoinGlass Data):** ตรวจสอบ Open Interest (OI), Funding Rates, โซนบังคับล้างพอร์ต (Liquidation Heatmaps), ดัชนีวาฬและความลึกตลาด (CGDI / CDRI), และ Coinbase Premium Index เพื่อหลีกเลี่ยงการเปิดสถานะฝั่งเดียวกับฝูงชนที่กำลังถูกล้างพอร์ต

### 4.3 Layer 3: Sentiment, Audit & Macro Context (Weight: 20%)

- ใช้ค่า Fear & Greed Index, Altcoin Season Index และดัชนีความผันผวนมหภาค (VIX) เป็นตัวปรับขนาดความเสี่ยง (Risk-On vs. Risk-Off)

## 5. Doctrine Guardrails, Exclusions & Veto Rules

สร้าง `DoctrineGuardrailService` เพื่อบังคับใช้กฎเหล็กดังต่อไปนี้:

1. **Data Source Security & Exclusions:**
   - ดึงข้อมูลแท่งเทียน CEX จาก Binance Spot `GET /api/v3/klines` ลงฐานข้อมูล SQLite ของระบบโดยตรง ห้ามเรียกผ่านพร็อกซี `dquery.sintral.io` เด็ดขาด
   - บล็อกการใช้ข้อมูลจาก `meme-rank`, `address-pnl-rank`, `trading-signal` (เหรียญมีมรายไม้), `binance-wallet-tracker`, `query-address-info` และ `binance-leaderboard`
2. **Token Audit Veto Rules:**
   - หาก `riskLevelEnum === "HIGH"`, มีรายละเอียดความเสี่ยงประเภท `RISK` (เช่น Honeypot) หรือมีภาษีขาย (`sellTax > 0`) -> สั่ง Veto และตัดเข้าสถานะ `FLAT` ทันที
   - หาก `riskLevelEnum === "MID"` (มีรายการเตือน `CAUTION` เช่น Mintable) -> ไม่ปัดตกทันที แต่ต้องแสดงรายการ `isHit` ทั้งหมดบน UI และหักคะแนนความมั่นใจลง
3. **Tokenized Securities Halt Check:**
   - สำหรับเหรียญหุ้น (เช่น Ondo Tokenized Stocks) ต้องตรวจสอบสถานะตลาดและรหัสพักการซื้อขาย (Earnings, Dividends, Splits, Mergers) ห้ามเปิดหรือถือสถานะข้ามช่วง Halt เด็ดขาด
4. **Portfolio Correlation & Drawdown Halt Kill-Switch:**
   - ตรวจสอบค่าสหสัมพันธ์ของเหรียญในพอร์ต (`Correlation of Book`) เพื่อป้องกันการเปิดไม้ซ้อนทับในทิศทางเดียวกันเกินเพดานความเสี่ยง
   - หากผลขาดทุนสะสมรายวันแตะเพดานที่กำหนด (`Drawdown Halt`) ให้สั่งยกเลิกออเดอร์ค้างทั้งหมด ปิดสถานะเข้าสู่ `FLAT` และล็อกระบบจนกว่าผู้ใช้จะส่งบันทึกทบทวน (`Written Review Memo`)
5. **Standardized Analysis Output:**
   - ทุกการวิเคราะห์ต้องส่งออกข้อมูล: (1) `Regime` (`TREND` | `RANGE` | `CHAOTIC`), (2) แผนจำลอง `2-3 Scenarios` พร้อมจุดราคา Invalidation ที่ชัดเจน (ห้ามทำนายราคาเป้าหมายแบบจุดเดียวลอยๆ), (3) ค่า `Confidence` (`0-1` หากต่ำกว่าเกณฑ์ขั้นต่ำให้ตัดเป็น `FLAT`), และ (4) ขนาดออเดอร์ที่คำนวณจาก Fractional Kelly

## 6. Implementation Checklist for Claude Opus 4.6 Thinking

- [ ] **Step 1:** อัปเดต Prisma / SQLite Schema สำหรับ `SignalEvaluationLog` และ `PortfolioRiskState`
- [ ] **Step 2:** สร้างไฟล์ `src/schemas/ev-edge.schema.ts` ด้วย Zod และบังคับแปลงตัวเลขผ่าน `Decimal`
- [ ] **Step 3:** สร้าง `src/repositories/ev-edge.repository.ts` พร้อมครอบ `handlePrismaError` ทุกจุด
- [ ] **Step 4:** พัฒนา `ev-calculator.service.ts`, `believability-scorer.service.ts` และ `doctrine-guardrail.service.ts`
- [ ] **Step 5:** สร้าง Server Actions ใน `src/actions/ev-edge.action.ts` พร้อมสั่ง `revalidatePath`
- [ ] **Step 6:** อัปเดต UI ในส่วน `ApprovalQueueTable` และเพิ่ม `RiskGuardrailBanner` เพื่อแสดงค่า Net $EV$, 2–3 Scenarios, คำเตือน Audit `MID` และหน้าจอปลดล็อก `Drawdown Halt`
- [ ] **Step 7:** เขียน Unit Tests สำหรับทุก Service และเขียน Playwright E2E Test สำหรับ Flow การสั่ง Veto (`FLAT`) และการล็อกระบบเมื่อเกิด `Drawdown Halt` จากนั้นรัน `npm run lint`, `npm test`, และ `npm run build` ให้ผ่านสมบูรณ์