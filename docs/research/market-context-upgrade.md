# Context: Upgrade Market Context Menu (Semi-Dynamic Fear & Greed Index for PWA)

## 1. Objective

อัปเกรดโมดูล **Market Context Menu (Fear & Greed Index)** ของระบบ **Antigravity** ให้มีความสามารถในการบ่งชี้สภาวะตลาดเชิงลึกแบบ **Semi-Dynamic (ล็อกส่วนหัว ปล่อยส่วนหาง)** โดยผสานนิยามทางจิตวิทยาตลาดที่คงที่ เข้ากับสาเหตุจากข่าวสารจริงผ่าน AI Intelligence และคำแนะนำแอคชันที่เชื่อมโยงกับตาราง **Approval Queue** พร้อมรองรับการใช้งานแบบ **Mobile-First PWA (Progressive Web App)** อย่างสมบูรณ์

## 2. Architecture & Doctrine Guardrails

- **Strict Layered Architecture:** บังคับใช้โครงสร้าง `Server Actions / Controllers` -> `Services` -> `Repositories` ห้ามเขียนลอจิกการผสมข้อความหรือดึงข้อมูลฐานข้อมูลลงใน UI Component โดยตรง
- **Trading Doctrine Compliance:**
  - ดัชนี Fear & Greed Index และ Social Sentiment เป็นเพียง **Context Layer** เพื่อประเมินความเสี่ยงและอารมณ์ตลาด ห้ามนำไปใช้เป็นตัวสั่งเปิดออเดอร์อัตโนมัติแบบแยกเดี่ยว (Never a standalone trigger)
  - คำแนะนำในระบบต้องยึดหลักการรักษาเงินทุน (Capital Preservation) และอ้างอิงค่าความมั่นใจ (Confidence Gate: `0.55` สำหรับ FLAT gate และ `0.70` สำหรับ High Confidence)
- **Validation & Error Handling:** ใช้ `Zod` ตรวจสอบโครงสร้างข้อมูลทุกครั้ง และดักจับข้อผิดพลาดในระดับ Repository ด้วย Error Handler มาตรฐาน

## 3. Semi-Dynamic String Concatenation Logic

ระบบต้องประกอบข้อความจาก 3 ส่วนเข้าด้วยกัน:

`[Static_Definition] + [Dynamic_Market_Cause] + [System_Action_Suggestion]`

### 3.1 Domain Score Mapping Specification

| Score Range | Status Label | Theme Color (Hex) | Static Definition (`Static_Definition`) | Default Action Template (`System_Action_Suggestion`) |
| :--- | :--- | :--- | :--- | :--- |
| **0 - 24** | Extreme Fear (กลัวจัด) | `#E53935` (Dark Red) | ตลาดอยู่ในภาวะกลัวจัด (Extreme Fear) สินทรัพย์โดนเทขายอย่างรุนแรงจากความตื่นตระหนก | แนะนำ: ตรวจสอบสถานะพอร์ตอย่างระมัดระวัง หลีกเลี่ยงการใช้อารมณ์ Panic Sell และจับตาดูสัญญาณแนวรับสำคัญในระบบ |
| **25 - 49** | Fear (กังวล) | `#FB8C00` (Orange) | ตลาดอยู่ในภาวะกังวล (Fear) นักลงทุนส่วนใหญ่ชะลอการซื้อขายเพื่อประเมินสถานการณ์ | แนะนำ: ป้องกันความเสี่ยงด้วยการถือเงินสด (Cash) บางส่วน และรอสัญญาณที่มีค่า Conf สูงกว่า 0.70 ในระบบก่อนพิจารณาเข้าออเดอร์ |
| **50** | Neutral (เป็นกลาง) | `#FDD835` (Yellow) | ตลาดอยู่ในภาวะเป็นกลาง (Neutral) แรงซื้อและแรงขายมีความสมดุล ราคามักวิ่งออกข้างเพื่อสะสมพลัง | แนะนำ: วางแผนกลยุทธ์รับ-ต้านล่วงหน้า และติดตามการอัปเดตจากโมดูล Intelligence & News Digest เพื่อจับจังหวะการ Breakout |
| **51 - 74** | Greed (โลภ) | `#4CAF50` (Green) | ตลาดอยู่ในภาวะโลภ (Greed) สินทรัพย์เป็นขาขึ้นที่แข็งแกร่ง นักลงทุนเริ่มไล่ราคาจากอาการกลัวตกขบวน (FOMO) | แนะนำ: หลีกเลี่ยงการเปิด Buy ไล่ราคาที่ยอดพอยท์ และตรวจสอบ Approval Queue ด้านบนเพื่อพิจารณา Approve คำสั่ง flat เพื่อทยอยล็อกกำไร |
| **75 - 100** | Extreme Greed (โลภจัด) | `#2E7D32` (Dark Green) | ตลาดอยู่ในภาวะโลภจัด (Extreme Greed) ราคาพุ่งสูงเกินมูลค่าพื้นฐาน และตลาดมีความร้อนแรงเกินไป | แนะนำ: ตลาดเสี่ยงต่อการเกิดการปรับฐานลึก (Correction) ในไม่ช้า ควรงดเปิดโพสิชันใหม่เพิ่ม และเน้นการปกป้องเงินทุนเป็นหลัก |

### 3.2 Dynamic Context Injection Rules

1. **`Dynamic_Market_Cause` (The "Why"):**
   - ดึงข้อมูลสรุปจากโมดูล `Intelligence & News Digest` / `Macro Context` ล่าสุด (1-2 Bullet points)
   - ตัวอย่าง (Score 63): *"สอดคล้องกับสัญญาณบวกจากข่าวดัชนีราคาผู้บริโภค หรือมีการสะสมสถานะ Long ของเหรียญหลัก เช่น BTC และ SOL อย่างต่อเนื่อง"*
2. **`System_Action_Suggestion` (The "Action"):**
   - ตรวจสอบรายการที่ค้างอยู่ใน `Approval Queue` ณ ขณะนั้น หากมีเหรียญที่มีสัญญาณชัดเจน (เช่น คำสั่ง `Flat` หรือ `Long` ที่มีค่า `Conf` ตามเกณฑ์) ให้ระบุชื่อเหรียญและค่า `Conf` ลงในคำแนะนำแบบไดนามิก
   - ตัวอย่าง: *"แนะนำ: หลีกเลี่ยงการเปิด Buy ไล่ราคา และตรวจสอบ Approval Queue ด้านบนสำหรับเหรียญ SOLUSDT (Conf 0.75) เพื่อพิจารณา Approve คำสั่ง Flat ล็อกกำไร"*

## 4. UI/UX & PWA Mobile-First Specification

### 4.1 Default Card View (Core Elements)

- **Desktop Layout:** แสดงกรอบการ์ดในส่วน Market Context ประกอบด้วย:
  1. `Header Label`: ข้อความ "Fear & Greed Index" โทนสีเทาอ่อน พร้อมไอคอน `ℹ️`
  2. `Score Visual`: ตัวเลขคะแนนขนาดใหญ่ เด่นชัด ใช้สีตามตาราง Domain Score Mapping
  3. `Status Badge`: ป้ายกำกับสถานะ เช่น `โลภ (Greed)`
- **Mobile PWA Layout (Collapsed State):**
  - แสดงผลแบบ **Collapsible Card** ความสูงกระชับ (`60px - 70px`) ในบรรทัดเดียว: `[ไอคอนเข็มไมล์จิ๋ว] -> Fear & Greed -> [ 63 ] โลภ (Greed) [ℹ️]` เพื่อประหยัดพื้นที่แนวตั้งให้มองเห็นตาราง `Approval Queue` ด้านบนได้พร้อมกัน
- **Safe Touch Target:** พื้นที่คลิก/แตะของการ์ดและปุ่มต้องมีขนาดไม่ต่ำกว่า `48px x 48px` ป้องกันการกดพลาดไปโดนเมนู `Altcoin Season Index` ข้างเคียง

### 4.2 Interactive Detail View (Desktop vs. Mobile PWA)

- **ห้ามใช้ `mouseover` เป็นหลักบนอุปกรณ์พกพาเด็ดขาด** ให้แยกพฤติกรรมตาม Viewport / Pointer Capability:
  - **Desktop (`hover: hover` & `min-width: 768px`):** เมื่อนำเมาส์ไปชี้ (Hover) หรือคลิกที่การ์ด/ไอคอน `ℹ️` ให้แสดง **Smart Tooltip / Popover** แบบ Fade-in
  - **Mobile/Tablet PWA (Touch Devices):** เมื่อใช้นิ้วแตะ (Tap to Open) ให้เปิดหน้าต่าง **Bottom Sheet (Drawer)** สไลด์ขึ้นมาจากขอบจอด้านล่างเต็มความกว้างหน้าจอ และปัดลงหรือแตะพื้นที่ว่างเพื่อปิด

### 4.3 Visual Blueprint Inside Tooltip / Bottom Sheet

จัดเรียงเนื้อหาให้อ่านจบภายใน 3 วินาที:

1. **Header:** `📊 Fear & Greed Index: {score} [ {status_label} ]`
2. **Section 1 - `[ความหมาย]`:** แสดงข้อความ `Static_Definition`
3. **Section 2 - `[สาเหตุตลาดวันนี้] 🤖 AI Intelligence`:** แสดงรายการ Bullet points จาก `Dynamic_Market_Cause` พร้อมระบุวันที่อัปเดตข้อมูล
4. **Section 3 - `[⚡ คำแนะนำระบบ Antigravity]`:** แสดงกล่องข้อความไฮไลต์ `System_Action_Suggestion`
5. **Quick Action Button (เฉพาะใน Bottom Sheet / Popover):** ปุ่มทางลัด `"ไปที่ Approval Queue ↑"` เมื่อกดแล้วจะปิด Bottom Sheet และ Smooth Scroll ขึ้นไปโฟกัสตาราง `Approval Queue` ด้านบนทันที

### 4.4 Micro-interactions, Aesthetics & PWA Performance

- **Count-up Animation:** ตัวเลขคะแนนวิ่งจาก `0` ไปยังค่าจริงอย่างรวดเร็วเมื่อโหลดหรืออัปเดตค่าใหม่
- **Neon Glow Effect:** เพิ่ม `text-shadow` และ `box-shadow` เรืองแสงจางๆ ตามสีของระดับคะแนน เพื่อตัดกับพื้นหลัง Dark Mode ของระบบ Antigravity
- **Haptic Feedback:** เรียกใช้ `navigator.vibrate(15)` (สั่นเบาๆ 10-20ms) เมื่อผู้ใช้แตะเปิด Bottom Sheet บนอุปกรณ์ที่รองรับ
- **PWA Caching Strategy (`Stale-While-Revalidate`):**
  - แสดงค่าดัชนีและข้อความล่าสุดจาก Local Cache ทันทีภายใน `0.1s` ที่เปิดแอป
  - ดึงข้อมูลใหม่จาก Server Action ในเบื้องหลัง (Background Sync) และเมื่อข้อมูลเปลี่ยนให้สั่ง `revalidatePath` / อัปเดต State พร้อมเล่นเอฟเฟกต์ Count-up

## 5. Implementation Checklist & Testing Requirements

- [x] สร้าง Zod Schema และ Type สำหรับ 5 โดเมนคะแนนและโครงสร้าง Semi-Dynamic String (`web/src/types/marketContext.ts`)
- [x] สร้าง Repository และ Service Layer สำหรับดึงข้อมูล AI Digest + Approval Queue มาประกอบข้อความ (`web/src/repositories/marketContextRepository.ts`, `web/src/services/marketContextService.ts`)
- [x] พัฒนา UI Component รองรับทั้ง Desktop Popover และ Mobile PWA Bottom Sheet (Touch Target >= 48x48px) (`web/src/components/MarketContextCard.tsx`)
- [x] เพิ่ม Count-up Animation, Neon Glow CSS, Haptic Feedback และปุ่มลัดโฟกัส Approval Queue (`web/src/controllers/useMarketContext.ts`, `web/src/index.css`, `web/src/components/ApprovalQueue.tsx`)
- [x] เขียน Unit Tests สำหรับ `MarketContextService` ครอบคลุมทั้ง 5 ช่วงคะแนนและกรณี Fallback เมื่อไม่มีข่าวใหม่ (`web/src/services/marketContextService.test.ts`)
- [x] ตรวจสอบความถูกต้องด้วยคำสั่ง `npm run lint` และ `npm run build` ให้ผ่านทั้งหมดโดยไม่มี TypeScript Error

---

## 6. Implementation & Verification Record

- **Implemented Files:**
  - `web/src/types/marketContext.ts`: Zod schemas & types สำหรับ 5 domain ranges, dynamic cause, action suggestion, และ semi-dynamic context model
  - `web/src/repositories/marketContextRepository.ts`: LocalStorage caching layer (<0.1s render), news digest bullet extractor, approval queue candidate finder
  - `web/src/services/marketContextService.ts`: Semi-dynamic concatenation service (`[Static_Definition] + [Dynamic_Market_Cause] + [System_Action_Suggestion]`)
  - `web/src/services/marketContextService.test.ts`: Node/tsx test suite 5/5 passing (ครอบคลุมทั้ง 5 โดเมนคะแนน, Fallbacks, Dynamic queue suggestions)
  - `web/src/controllers/useMarketContext.ts`: Hook จัดการ responsive drawer/popover state, count-up animation, haptic vibration (`navigator.vibrate(15)`), scroll-to-queue
  - `web/src/components/MarketContextCard.tsx`: Responsive component (Desktop Popover vs Mobile Bottom Sheet Drawer พร้อมปุ่มทางลัด "ไปที่ Approval Queue ↑")
  - `web/src/components/ApprovalQueue.tsx`: เพิ่ม target anchor `id="approval-queue"` สำหรับ smooth scroll
  - `web/src/pages/HomePage.tsx`: อัปเดต Home Page ให้ใช้ `MarketContextCard` modular component
  - `web/src/index.css`: เพิ่มคลาสอนิเมชัน `.touch-target-safe`, `@keyframes slideUp`, `@keyframes fadeIn`
- **Verification:**
  - `web/`: `npm run test` -> 5 passed (100%)
  - `web/`: `npm run build` -> `tsc -b && vite build` passed with 0 errors
  - `root`: pytest unit tests 55/55 passed