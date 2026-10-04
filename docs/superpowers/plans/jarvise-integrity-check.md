# Context: Jarvise Intelligence Trade - System Integrity & Completeness Audit

## 1. System Overview & Objective
เป้าหมายของการตรวจสอบ: สแกนและวิเคราะห์ Source Code ของระบบเทรดอัตโนมัติ "Jarvise" เพื่อประเมินความพร้อมใช้งานจริง (Production-ready) โดยมุ่งเน้นที่ความปลอดภัยของเงินทุน (Capital Safety), ความเสถียรของการเชื่อมต่อ API, ความแม่นยำของ Trading Logic และโครงสร้างโค้ดที่ถูกต้องตามมาตรฐาน

## 2. Data Integration & Analytics Validity
อ้างอิงจากเอกสาร "Jarvise : Crypto Trader _2" ระบบจะต้องมีการประมวลผลข้อมูลที่ถูกต้องและครบถ้วน[cite: 2]:
- [ ] **Market Overview Sources:** ตรวจสอบการเชื่อมต่อและการจัดการข้อมูลจาก TradingView (Price charts, Order books), CoinMarketCap (Fear & Greed Index, Altcoin Season Index), และ CoinGlass (Derivatives, Liquidations, Funding rates)[cite: 2]
- [ ] **Market Depth & Liquidity:** ตรวจสอบว่ามีกลไกในการอ่าน Market Depth เพื่อคำนวณและลดความเสี่ยงจาก Slippage โดยเฉพาะในช่วงที่ตลาดมีความผันผวนสูง (High Volatility)[cite: 2]
- [ ] **Technical Indicators:** ตรวจสอบฟังก์ชันคณิตศาสตร์ที่ใช้คำนวณตัวชี้วัด เช่น RSI, MACD, Bollinger Bands, และ ATR ว่าให้ค่าที่แม่นยำตรงตามมาตรฐานการวิเคราะห์ทางเทคนิค[cite: 2]

## 3. Architecture & Code Structure Strict Rules
- [ ] **Layered Architecture:** บังคับใช้โครงสร้างแบบแบ่งชั้นอย่างเคร่งครัด (Controllers ➡️ Services ➡️ Repositories) ห้ามเรียกใช้ Prisma ORM หรือ Database Client โดยตรงจาก Server Components หรือ Controllers เด็ดขาด การเข้าถึงข้อมูลทั้งหมดต้องผ่าน Repository Layer เท่านั้น
- [ ] **Validation & Security:** ตรวจสอบว่ามีการใช้ Zod schemas ในการ Validate ข้อมูลทั้งฝั่ง Input (Signal, Order parameters) และ Output 
- [ ] **Credentials Management:** API Keys ของ Exchange และ Database URIs ทั้งหมดต้องถูกเรียกผ่าน Environment Variables (`.env`) ห้ามมีการ Hardcode เด็ดขาด

## 4. Risk Management & Execution Engine
- [ ] **The 1% Rule & Position Sizing:** ยืนยันว่าสมการคำนวณ Position Sizing จะไม่ทำให้ความเสี่ยงเกิน 1% ของเงินทุนทั้งหมดต่อการเทรด 1 ครั้ง ไม่ว่าในกรณีใดๆ
- [ ] **Invalidation & Drawdown Halt:** ตรวจสอบระบบบังคับ Stop-Loss (S/L) และฟังก์ชัน Drawdown Halt ที่จะหยุดการทำงานของบอททันทีเมื่อพอร์ตขาดทุนถึงขีดจำกัดรายวันที่กำหนดไว้
- [ ] **State & Transaction Integrity:** การบันทึกสถานะของออเดอร์ในฐานข้อมูล (Pending, Filled, Canceled) ต้องทำงานสอดคล้องกับสถานะจริงบน Exchange และต้องใช้ Database Transactions (ACID) เพื่อป้องกันข้อมูล Ledger คลาดเคลื่อน

## 5. Required Testing Strategy (CRITICAL)
ก่อนที่จะมีการปรับปรุงหรือเพิ่มโค้ดใดๆ ต้องมีการวางแผนการทดสอบดังนี้:
1. **Service / Server Action Tests (Unit/Integration):** REQUIRED สำหรับทุกๆ Business Logic ใหม่, การคำนวณ Signal/Indicators, กลไกการจัดการความเสี่ยง (Risk Management), และ Database Mutations 
2. **E2E Tests:** ระบบการส่งคำสั่งซื้อขาย (Order Execution) ถือเป็น "Critical User Journey" (CUJ) อย่างแท้จริง ดังนั้นต้องมีการร่างแผน E2E Test (เช่นผ่าน Playwright หรือโหมด Paper Trading / Simulation) เพื่อจำลองตั้งแต่จังหวะรับ Signal ไปจนถึงการส่ง API ไปยัง Exchange และบันทึกผลลง Database

## 6. AI Execution Directive (คำสั่งดำเนินการสำหรับ AI)
เมื่อรับทราบ Context นี้แล้ว ให้ Cursor ดำเนินการดังนี้:
1. สแกน Source Code ของโปรเจกต์ทั้งหมดเพื่อตรวจสอบความสอดคล้องกับข้อกำหนดข้อ 2 ถึง 5
2. ก่อนทำการเขียนหรือแก้ไขไฟล์ใดๆ **คุณต้อง (MUST)** สรุปแผนการดำเนินการ (Technical Implementation Plan) ซึ่งประกอบด้วย: การเปลี่ยนแปลง Schema, ไฟล์ที่จะสร้าง/แก้ไขใหม่, Server Action Signatures, และแผนการทดสอบ (Testing Strategy) ตามที่ระบุไว้ในข้อ 5 เพื่อให้ User ตรวจสอบลอจิกก่อน
3. สร้างรายงานสรุปจุดเสี่ยง (Vulnerabilities), จุดบกพร่องทางลอจิกของการเทรด, และการละเมิดกฎสถาปัตยกรรม พร้อมนำเสนอโค้ดตัวอย่างสำหรับการแก้ไข (Refactoring)