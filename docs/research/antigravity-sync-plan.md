# Master Implementation Plan: Jarvise Model Routing & Market Context PWA Upgrade

## 1. Overview & Execution Order

เอกสารชุดนี้คือ Master Blueprint สำหรับซิงค์เข้าสู่ **Antigravity IDE** เพื่ออัปเกรดระบบ **Jarvise Autonomous Trader** ออกเป็น 2 เฟสที่เชื่อมโยงกัน:

1. **Phase 1: OpenRouter Multi-Tier Model Routing (`openclaw.json` & Gateway Service)**
2. **Phase 2: Semi-Dynamic Market Context Menu (Fear & Greed Index for Mobile-First PWA)**

## 2. Global Architecture & Doctrine Guardrails

- **Strict Layered Architecture:** บังคับใช้โครงสร้าง `Controllers / Server Actions` -> `Services` -> `Repositories` อย่างเคร่งครัด ห้ามเรียกใช้ Database (SQLite/Prisma) หรือยิง OpenRouter API โดยตรงจาก UI Component เด็ดขาด
- **Error Handling & Revalidation:**
  - ทุกฟังก์ชันใน Repository ต้องห่อหุ้มด้วย Error Handler มาตรฐาน (`handlePrismaError` / Database Error Wrapper)
  - ทุก Server Action ที่มีการเปลี่ยนแปลงข้อมูล (Mutation / Sync) ต้องเรียกใช้ `revalidatePath` และซิงค์กับ `router.refresh()` เสมอ
- **Jarvise Core Operating Doctrine:**
  - **Capital Preservation Beats Activity:** ความเสี่ยงต่อไม้ห้ามเกิน `1%` ของพอร์ต หากความมั่นใจต่ำกว่าเกณฑ์ `0.55` หรือระบบ AI เกิดข้อผิดพลาด ต้องตัดเข้าสถานะ `FLAT` ทันที
  - **Context Only:** ดัชนี Fear & Greed Index และกระแสโซเชียลเป็นเพียงข้อมูลประกอบ (Context) ห้ามใช้เป็นตัวสั่งเปิดออเดอร์แบบแยกเดี่ยว (Never a standalone trigger)
  - **Decimal Precision:** การคำนวณตัวเลขทางการเงิน ค่า $EV$, และระยะ ATR ต้องแปลงจาก String เป็น `Decimal` ก่อนคำนวณเสมอ

---

## 3. Phase 1 Specification: OpenRouter Multi-Tier Model Routing

### 3.1 Agent Profiles & Model Matrix (`openclaw.json`)

| Agent Role Enum | Primary OpenRouter Model | Fallback Models | Temp | Core Task |
| :--- | :--- | :--- | :--- | :--- |
| `DOCTRINE_JUDGE` | `anthropic/claude-sonnet-5` | `anthropic/claude-sonnet-4.6`, `anthropic/claude-opus-5.5` | `0.0` | ตรวจสอบกฎ 1% Risk, Token Audit Veto (`HIGH` risk -> `FLAT`), และเงื่อนไข Pullback 6 ข้อ |
| `HEARTBEAT_INGEST` | `deepseek/deepseek-v4.1-flash` | `minimax/minimax-m3`, `google/gemini-3.8-flash` | `0.1` | รันสแกนตลาด 24/7 รอบละ 30 นาที ดึงข้อมูล TradingView, CMC, และ CoinGlass V4 API |
| `QUANT_ANALYZER` | `deepseek/deepseek-v4-pro` | `openai/gpt-5.6-luna`, `anthropic/claude-sonnet-5` | `0.0` | คำนวณสมการ $EV$, ระยะ Stop-Loss ($\ge 1.5 \times \text{ATR}$), และสร้าง JSON Signal |
| `CONTEXT_DIGEST` | `google/gemini-3.8-flash` | `z-ai/glm-5.3-flash`, `deepseek/deepseek-v4.1-flash` | `0.3` | สรุปข่าวสาร Macro และสร้างข้อความภาษาไทยแบบ Semi-Dynamic สำหรับ Fear & Greed Index |
| `PAPER_SANDBOX` | `nvidia/nemotron-3-ultra-550b-a55b:free` | `nvidia/nemotron-3-super:free`, `google/gemini-3.8-flash` | `0.1` | โหมดทดสอบระบบจำลอง (Paper Trading) แบบไม่เสียค่าใช้จ่าย |

### 3.2 Cost & Reliability Engineering

- เปิดใช้งาน `cache_control: { type: "ephemeral" }` สำหรับ System Prompt ที่แนบไฟล์ `jarvise-doctrine.txt`
- ผูกสกิล `claw-compactor` เพื่อบีบอัดประวัติ Session อัตโนมัติเมื่อขนาดเกิน `300KB`
- ส่งอาเรย์ `models: [primaryModel, ...fallbackModels]` ใน OpenRouter Request เพื่อให้สลับโมเดลสำรองอัตโนมัติเมื่อเกิด Rate Limit

---

## 4. Phase 2 Specification: Market Context Menu (Semi-Dynamic PWA)

### 4.1 String Concatenation Formula

ประกอบข้อความแสดงผลจาก 3 ส่วน:

`[Static_Definition] + [Dynamic_Market_Cause] + [System_Action_Suggestion]`

### 4.2 Domain Score Mapping Table

| Score Range | Status Badge | Hex Color | `Static_Definition` | Default `System_Action_Suggestion` |
| :--- | :--- | :--- | :--- | :--- |
| **0 - 24** | Extreme Fear (กลัวจัด) | `#E53935` | ตลาดอยู่ในภาวะกลัวจัด (Extreme Fear) สินทรัพย์โดนเทขายอย่างรุนแรงจากความตื่นตระหนก | แนะนำ: ตรวจสอบสถานะพอร์ตอย่างระมัดระวัง หลีกเลี่ยงการใช้อารมณ์ Panic Sell และจับตาดูสัญญาณแนวรับสำคัญในระบบ |
| **25 - 49** | Fear (กังวล) | `#FB8C00` | ตลาดอยู่ในภาวะกังวล (Fear) นักลงทุนส่วนใหญ่ชะลอการซื้อขายเพื่อประเมินสถานการณ์ | แนะนำ: ป้องกันความเสี่ยงด้วยการถือเงินสด (Cash) บางส่วน และรอสัญญาณที่มีค่า Conf สูงกว่า 0.70 ในระบบก่อนพิจารณาเข้าออเดอร์ |
| **50** | Neutral (เป็นกลาง) | `#FDD835` | ตลาดอยู่ในภาวะเป็นกลาง (Neutral) แรงซื้อและแรงขายมีความสมดุล ราคามักวิ่งออกข้างเพื่อสะสมพลัง | แนะนำ: วางแผนกลยุทธ์รับ-ต้านล่วงหน้า และติดตามการอัปเดตจากโมดูล Intelligence & News Digest เพื่อจับจังหวะการ Breakout |
| **51 - 74** | Greed (โลภ) | `#4CAF50` | ตลาดอยู่ในภาวะโลภ (Greed) สินทรัพย์เป็นขาขึ้นที่แข็งแกร่ง นักลงทุนเริ่มไล่ราคาจากอาการกลัวตกขบวน (FOMO) | แนะนำ: หลีกเลี่ยงการเปิด Buy ไล่ราคาที่ยอดพอยท์ และตรวจสอบ Approval Queue ด้านบนเพื่อพิจารณา Approve คำสั่ง flat เพื่อทยอยล็อกกำไร |
| **75 - 100** | Extreme Greed (โลภจัด) | `#2E7D32` | ตลาดอยู่ในภาวะโลภจัด (Extreme Greed) ราคาพุ่งสูงเกินมูลค่าพื้นฐาน และตลาดมีความร้อนแรงเกินไป | แนะนำ: ตลาดเสี่ยงต่อการเกิดการปรับฐานลึก (Correction) ในไม่ช้า ควรงดเปิดโพสิชันใหม่เพิ่ม และเน้นการปกป้องเงินทุนเป็นหลัก |

### 4.3 Mobile-First PWA & UX Specifications

- **Responsive Card Layout:**
  - **Desktop:** แสดงหัวข้อ `Fear & Greed Index`, ตัวเลขขนาดใหญ่พร้อม Neon Glow, Status Badge และไอคอน `ℹ️` (Hover/Click เพื่อเปิด Smart Popover)
  - **Mobile PWA:** แสดงแบบ **Collapsible Card** สูง `60px - 70px` และมีพื้นที่แตะ (Safe Touch Target) ไม่ต่ำกว่า `48px x 48px` ห้ามใช้ `mouseover` บนมือถือเด็ดขาด
- **Bottom Sheet & Haptic Feedback (Mobile):**
  - เมื่อแตะการ์ดบนมือถือ ให้สั่งสั่นเตือนผ่าน `navigator.vibrate(15)` และสไลด์ **Bottom Sheet** ขึ้นมาจากขอบจอด้านล่าง
  - แสดงเนื้อหาแยก 3 ส่วน: `[ความหมาย]`, `[สาเหตุตลาดวันนี้] 🤖 AI Intelligence` (ดึงผ่าน Agent `CONTEXT_DIGEST`), และ `[⚡ คำแนะนำระบบ Antigravity]` (ดึงชื่อเหรียญและค่า `Conf` จากตาราง `Approval Queue` จริง)
  - มีปุ่มทางลัด `"ไปที่ Approval Queue ↑"` สำหรับกดเพื่อปิด Bottom Sheet แล้ว Smooth Scroll ขึ้นไปโฟกัสตารางคำสั่งซื้อขายด้านบนทันที
- **Stale-While-Revalidate Caching:**
  - โหลดค่าล่าสุดจาก Cache มาแสดงภายใน `0.1s` พร้อมเอฟเฟกต์ **Count-up Animation** จาก `0` ไปยังค่าจริง และดึงข้อมูลใหม่เบื้องหลัง (Background Sync)

---

## 5. Step-by-Step Execution Checklist for Antigravity Agent

- [ ] **Step 1:** อัปเดตไฟล์ `openclaw.json` และสร้าง `src/schemas/model-router.schema.ts` + `src/schemas/market-context.schema.ts`
- [ ] **Step 2:** สร้าง Repository Layer (`ai-usage-log.repository.ts` และ `market-context.repository.ts`) พร้อมระบบจัดการ Error มาตรฐาน
- [ ] **Step 3:** สร้าง Service Layer (`openrouter-gateway.service.ts` และ `market-context.service.ts`) เชื่อมต่อโมเดล `CONTEXT_DIGEST` เข้ากับระบบผสมข้อความ 3 ส่วน
- [ ] **Step 4:** สร้าง Server Actions (`model-routing.action.ts` และ `market-context.action.ts`) พร้อมบังคับใช้ `revalidatePath`
- [ ] **Step 5:** พัฒนา UI Components (`MarketContextCard.tsx`, `SentimentDetailOverlay.tsx`, `useHapticFeedback.ts`) รองรับทั้ง Desktop Popover และ Mobile PWA Bottom Sheet
- [ ] **Step 6:** เขียน Unit & Integration Tests สำหรับ `OpenRouterGatewayService` และ `MarketContextService` ให้ครบทุกเงื่อนไข
- [ ] **Step 7:** รันคำสั่ง `npm run lint`, `npm test`, และ `npm run build` เพื่อยืนยันว่าระบบไม่มีข้อผิดพลาด