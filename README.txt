===============================================
  AYPEE INDUSTRIES - Quotation Management System
===============================================

WHAT THIS IS:
  A complete web-based quotation system with:
  - Login/Registration system
  - Customer management
  - Item catalog (13 pre-loaded chain products)
  - Interactive quotation builder
  - PDF export with your Aypee Industries letterhead

HOW TO RUN:
  1. Make sure Python is installed (python.org/downloads)
  2. Open this folder in Command Prompt:
     cd "C:\Users\hp\...\Aypee_Quotation_System"
  3. Install reportlab (for PDF):
     pip install reportlab
  4. Start the server:
     python server.py
     (or double-click START_SERVER.bat)
  5. Open browser: http://localhost:5000
  6. Login: admin / admin123

FILES:
  server.py          - Main web server (run this)
  database.py        - Database setup and models
  pdf_generator.py   - PDF generation with letterhead
  static/css/style.css - UI styling
  templates/         - HTML templates

TO ADD ITEMS:
  Login -> Items Catalog -> Add Item
  Fill in: name, description, specification, unit, rate

TO ADD CUSTOMERS:
  Login -> Customers -> Add Customer
  Fill in: contact person, company, address, phone, GSTIN

TO CREATE A QUOTATION:
  Login -> Quotations -> New Quotation
  1. Select customer from dropdown
  2. Pick items from catalog, enter quantity, click Add
  3. Review summary on the right
  4. Click "Create Quotation"
  5. Download PDF from the quotation detail page

DEFAULT ITEMS (pre-loaded):
  - Sugar Elevator Chain 66.60 MM  - Rs. 2,700/Mtr.
  - Double Strand Pintle Chain     - Rs. 3,200/Mtr.
  - G Type Chain                   - Rs. 4,500/Mtr.
  - Roller Chain                   - Rs. 550/Mtr.
  - Attachment Chain               - Rs. 1,800/Mtr.
  - Pintle Chain                   - Rs. 2,950/Mtr.
  - Crank Chain                    - Rs. 2,450/Mtr.
  - Bagasse Carrier Chain          - Rs. 2,850/Mtr.
  - Forged Chain                   - Rs. 3,600/Mtr.
  - Slat Chain                     - Rs. 1,250/Mtr.
  - Bucher Chain                   - Rs. 3,100/Mtr.
  - Bucher Harvesting Chain        - Rs. 3,400/Mtr.
  - Steel Bushd Roller Chain       - Rs. 3,200/Mtr.

DEFAULT CUSTOMERS (pre-loaded):
  - Novel Sugar Limited, Pilibhit
  - Ranipur Sugar Mills, Bijnor
  - KCSM Sugar Ltd, Gujarat
  - Punjab Sugar Mills, Moga
  - TPT Sugar Ltd, Tiruchirapalli
  - Arignar Anna Sugar Mills, Cuddalore
  - Vellore Sugar Co., Vellore
  - Chengalpattu Sugar Mills
  - Dharmapuri Sugar Mills

TROUBLESHOOTING:
  - "Python not found": Install Python from python.org and check
    "Add Python to PATH" during installation
  - "pip not found": Try "python -m pip install reportlab"
  - Port 5000 in use: Change PORT in server.py to 8080
  - Database error: Delete quotation.db and restart server
