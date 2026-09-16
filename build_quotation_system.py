import openpyxl
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.page import PageMargins

wb = openpyxl.Workbook()

RED="CC0000"; DARK_RED="8B0000"; BLUE="0000CC"; BLACK="000000"
LIGHT_GRAY="F2F2F2"; HDR_BG="E8E8E8"; BORDER_CLR="B0B0B0"

def f(name="Times New Roman", sz=11, bold=False, italic=False, underline=None, color=None):
    return Font(name=name, size=sz, bold=bold, italic=italic,
                underline=underline, color=color)

def a(h='left', v='center', wrap=False, indent=0):
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap, indent=indent)

def bdr():
    s = Side(style='thin', color=BORDER_CLR)
    return Border(left=s, right=s, top=s, bottom=s)

thick_bottom = Border(bottom=Side(style='medium', color=BLACK))
bottom_b = Border(bottom=Side(style='thin', color=BORDER_CLR))
thin_all = Border(
    left=Side(style='thin', color=BORDER_CLR),
    right=Side(style='thin', color=BORDER_CLR),
    top=Side(style='thin', color=BORDER_CLR),
    bottom=Side(style='thin', color=BORDER_CLR)
)

def sc(ws, r, c, v=None, font=None, align=None, fill=None, border=None, nf=None):
    cell = ws.cell(row=r, column=c, value=v)
    if font:   cell.font = font
    if align:  cell.alignment = align
    if fill:   cell.fill = PatternFill(start_color=fill, end_color=fill, fill_type='solid')
    if border: cell.border = border
    if nf:     cell.number_format = nf
    return cell

def mg(ws, r1, c1, r2, c2, v=None, font=None, align=None, fill=None, border=None):
    ws.merge_cells(start_row=r1, start_column=c1, end_row=r2, end_column=c2)
    c = ws.cell(row=r1, column=c1)
    if v is not None: c.value = v
    if font:   c.font = font
    if align:  c.alignment = align
    if fill:   c.fill = PatternFill(start_color=fill, end_color=fill, fill_type='solid')
    if border: c.border = border
    return c

def row_bdr(ws, r, c1, c2, bdr_obj):
    for c in range(c1, c2+1):
        ws.cell(row=r, column=c).border = bdr_obj

# =================================================================
# SHEET 1: QUOTATION
# =================================================================
ws = wb.active
ws.title = "QUOTATION"

col_w = {'A':2,'B':7,'C':22,'D':32,'E':9,'F':8,'G':13,'H':13,'I':8,'J':14,'K':1.2}
for col, w in col_w.items():
    ws.column_dimensions[col].width = w

ws.sheet_properties.pageSetUpPr.fitToPage = True
ws.page_setup.paperSize = ws.PAPERSIZE_A4
ws.page_setup.orientation = 'portrait'
ws.page_setup.fitToHeight = 1
ws.page_setup.fitToWidth = 1
ws.page_margins = PageMargins(left=0.3, right=0.3, top=0.3, bottom=0.3)

for r in range(22,37): ws.row_dimensions[r].height = 60
for r,h in {1:14,2:20,3:28,4:16,5:13,6:13,7:13,8:13,9:15,10:14,11:4,
            12:16,13:14,14:14,15:14,16:4,17:16,18:13,19:13,20:4,21:17,
            37:15,38:4,39:15,40:17,41:4,
            42:14,43:13,44:13,45:13,46:13,47:4,
            48:14,49:14,50:4,51:14,52:14}.items():
    ws.row_dimensions[r].height = h

# ── HEADER (matches Proforma Invoice header style) ──
mg(ws,1,2,1,5,"Subject to Jalandhar Jurisdiction", f(sz=10,underline='single'), a('left'))
mg(ws,1,8,1,10,"GSTIN: 03AAHFA4106G1Z4", f(sz=10), a('right'))

mg(ws,2,2,2,10,"QUOTATION", f(sz=16,bold=True), a('center'))

mg(ws,3,2,3,10,"AYPEE  INDUSTRIES", f(sz=22,bold=True,color=RED), a('center'))

mg(ws,4,2,4,4,"Correspondence Address:", f(sz=10,bold=True,underline='single'), a('left'))

red_side = Side(style='thin', color=RED)
chains_border = Border(left=red_side, right=red_side)
mg(ws,4,5,8,6,"CHAINS", f(sz=15,bold=True,color=RED), a('center','center'), border=chains_border)
for rr in range(4,9):
    row_bdr(ws, rr, 5, 6, chains_border)

for i,ln in enumerate(["STREET NO.1, ANAND NAGAR","NEAR KIDZ NEST SCHOOL,",
    "MAKSUDAN, JALANDHAR  (PUNJAB), INDIA","PIN CODE – 144008"], start=5):
    mg(ws,i,2,i,4,ln,f(sz=9),a('left'))

# Contact info bottom-aligned within the address block (rows 7-8, like Proforma)
mg(ws,7,8,7,10,"(Off.): 0181-2672260",f(sz=10,bold=True),a('right','bottom'))
mg(ws,8,8,8,10,"(Mob): 9357255758, 9041881996",f(sz=10,bold=True),a('right','bottom'))

row_bdr(ws,8,2,10,thick_bottom)

mg(ws,9,2,9,10,
   "MFG. OF ALL KINDS OF S.G. IRON CHAINS, FABRICATED CANE CARRIER, RBC & RAKE CARRIER CHAINS, SLAT & SPARE PARTS",
   f(sz=9,bold=True),a('center'))
row_bdr(ws,9,2,10,bottom_b)

mg(ws,10,2,10,3,"E-mail:",f(sz=10),a('left'))
sc(ws,10,4,"aypeeinds@yahoo.com",f(sz=10,underline='single',color=BLUE),a('left'))
mg(ws,10,8,10,9,"Date:",f(sz=10,bold=True),a('right'))
c_dt = sc(ws,10,10,"=TODAY()",f(sz=10,bold=True),a('right'),
          border=Border(bottom=Side(style='thin',color=BLACK),top=Side(style='thin',color=BLACK)),
          nf='DD - MM - YYYY')

# ── CUSTOMER SECTION ──
mg(ws,12,2,12,3,"To",f(sz=11,bold=True),a('left'))
sc(ws,13,2,None,f(sz=11,bold=True),a('left'))
sc(ws,14,2,'=IFERROR(INDEX(Customer_List!$B$2:$B$20,MATCH(B13,Customer_List!$A$2:$A$20,0)),"")',
   f(sz=11),a('left'))
sc(ws,15,2,'=IFERROR(INDEX(Customer_List!$C$2:$C$20,MATCH(B13,Customer_List!$A$2:$A$20,0)),"")',
   f(sz=11),a('left'))

cust_dv = DataValidation(type='list',formula1="='Customer_List'!$A$2:$A$20",
    allow_blank=True,showDropDown=False)
cust_dv.prompt="Select a customer";cust_dv.showInputMessage=True
ws.add_data_validation(cust_dv); cust_dv.add(ws['B13'])

mg(ws,17,2,17,10,"Sub: Quotation for _________________",f(sz=11,bold=True,underline='single'),a('center'))
sc(ws,18,2,"Dear Sir,",f(sz=11),a('left'))
mg(ws,19,2,19,10,
   "We are pleased to quote you the best rates for your inquiry. "
   "Thank you for your interest in our products.",f(sz=11),a('left',wrap=True))

# ── TABLE HEADER ──
for i,h in enumerate(["Sr. No.","Item","Description &\nSpecification","Qty.","Unit",
                       "Rate\n(Rs./unit)","Amount\n(Rs.)","GST %","Net Total\n(Rs.)"]):
    sc(ws,21,2+i,h,f(sz=9,bold=True),a('center',wrap=True),HDR_BG,thin_all)

# ── DATA VALIDATIONS ──
item_dv = DataValidation(type='list',formula1="='Item_Catalog'!$A$2:$A$20",
    allow_blank=True,showDropDown=False)
item_dv.prompt="Select an item";ws.add_data_validation(item_dv)
unit_dv = DataValidation(type='list',formula1='"Mtr.,Pcs.,Kg.,Set,Ltr.,Rmt.,Nos."',
    allow_blank=True,showDropDown=False);ws.add_data_validation(unit_dv)
gst_dv = DataValidation(type='list',formula1='"0,5,12,18,28"',
    allow_blank=True,showDropDown=False);ws.add_data_validation(gst_dv)

# ── DATA ROWS ──
IC="'Item_Catalog'"
for row in range(22,37):
    r=row
    sc(ws,r,2,r-21,f(sz=10,bold=True),a('center'),border=thin_all)
    c_it=sc(ws,r,3,None,f(sz=9,bold=True),a('left',wrap=True),border=thin_all)
    item_dv.add(c_it)
    sc(ws,r,4,
       f'=IFERROR(INDEX({IC}!$B$2:$B$20,MATCH(C{r},{IC}!$A$2:$A$20,0))&CHAR(10)&"Specification:"&CHAR(10)&INDEX({IC}!$D$2:$D$20,MATCH(C{r},{IC}!$A$2:$A$20,0)),"")',
       f(sz=8),a('left',wrap=True),border=thin_all)
    sc(ws,r,5,None,f(sz=10),a('center'),border=thin_all)
    sc(ws,r,6,f'=IFERROR(INDEX({IC}!$E$2:$E$20,MATCH(C{r},{IC}!$A$2:$A$20,0)),"")',
       f(sz=9),a('center'),border=thin_all)
    c_rt=sc(ws,r,7,f'=IFERROR(INDEX({IC}!$F$2:$F$20,MATCH(C{r},{IC}!$A$2:$A$20,0)),"")',
       f(sz=10),a('right'),border=thin_all,nf='#,##0.00')
    c_am=sc(ws,r,8,f'=IF(AND(E{r}<>"",G{r}<>""),E{r}*G{r},"")',
       f(sz=10),a('right'),border=thin_all,nf='#,##0.00')
    c_gs=sc(ws,r,9,18,f(sz=9),a('center'),border=thin_all)
    gst_dv.add(c_gs)
    c_nt=sc(ws,r,10,f'=IF(H{r}<>"",ROUND(H{r}*(1+I{r}/100),2),"")',
       f(sz=10,bold=True),a('right'),border=thin_all,nf='#,##0.00')

# ── TOTALS ──
mg(ws,37,2,37,7,"Sub-Total:",f(sz=10,bold=True),a('right'))
c_sub=sc(ws,37,8,'=SUMPRODUCT((H22:H36<>"")*(H22:H36))',f(sz=10,bold=True),a('right'),nf='#,##0.00')
row_bdr(ws,37,8,8,thick_bottom)

mg(ws,39,2,39,7,"GST (18%):",f(sz=10,bold=True),a('right'))
sc(ws,39,8,'=IF(H37<>"",ROUND(H37*0.18,2),"")',f(sz=10,bold=True),a('right'),nf='#,##0.00')

mg(ws,40,2,40,7,"Grand Total:",f(sz=11,bold=True,color=RED),a('right'))
c_gt=sc(ws,40,8,'=IF(H37<>"",H37+H39,"")',f(sz=12,bold=True,color=RED),a('right'),
    border=Border(bottom=Side(style='medium',color=BLACK),top=Side(style='medium',color=BLACK)),nf='#,##0.00')

# ── TERMS ──
terms=["Terms and Conditions",
       "1.  Rates are Ex-works.",
       "2.  GST 18% Extra as applicable.",
       "3.  Payment: 20% Advance and Balance against delivery.",
       "4.  Delivery: 10 – 12 weeks after receipt of order."]
for i,t in enumerate(terms):
    mg(ws,42+i,2,42+i,10,t,f(sz=10,bold=(i==0)),a('left'))

# ── SIGNATURE ──
mg(ws,48,2,48,5,"Thanks & Regards",f(sz=11,bold=True),a('left'))
mg(ws,49,2,49,5,"For Aypee Industries",f(sz=11,bold=True),a('left'))
mg(ws,51,2,51,5,"Raman Budhia (Partner)",f(sz=11,bold=True),a('left'))
mg(ws,52,2,52,5,"+91-9357255758",f(sz=11,bold=True),a('left'))

ws.print_area="A1:J53"
ws.column_dimensions['K'].hidden=True
print("QUOTATION sheet done")

# =================================================================
# SHEET 2: ITEM_CATALOG
# =================================================================
ws2=wb.create_sheet("Item_Catalog")
for col,w in {'A':25,'B':30,'C':25,'D':40,'E':8,'F':14}.items():
    ws2.column_dimensions[col].width=w

for i,h in enumerate(["Item_Name","Description","Short_Description","Specification","Unit","Rate_Per_Unit"]):
    c=ws2.cell(row=1,column=i+1,value=h)
    c.font=f(sz=10,bold=True,color="FFFFFF");c.fill=PatternFill(start_color=DARK_RED,end_color=DARK_RED,fill_type='solid')
    c.alignment=a('center');c.border=thin_all

items=[
["SUGAR ELEVATOR CHAIN","Sugar Elevator Chain 66.60 MM Pitch","Sugar Elevator 66.60",
 "PLATE THICKNESS : 5 MM\nPIN DIA : 11 MM\nBUSH DIA : 16 MM\nROLLER DIA : 24 MM\nSIDE LINK : 32 X 5 MM\nATTACHMENT LINK : 65 X 5 MM / 75 X 5 MM","Mtr.",2700],
["DOUBLE STRAND PINTLE CHAIN","Double Strand Pintle Chain 50.80 MM Pitch","DSP Chain 50.80",
 "PLATE THICKNESS : 6 MM\nPIN DIA : 14 MM\nBUSH DIA : 18 MM\nSIDE LINK : 38 X 6 MM\nATTACHMENT : 70 X 6 MM","Mtr.",3200],
["G TYPE CHAIN","G Type Welded Steel Chain 150 MM Pitch","G Type 150",
 "PLATE THICKNESS : 10 MM\nPIN DIA : 18 MM\nBUSH DIA : 24 MM\nROLLER DIA : 38 MM\nSIDE LINK : 50 X 10 MM","Mtr.",4500],
["ROLLER CHAIN","Standard Roller Chain 25.40 MM Pitch","Roller Chain 25.40",
 "PLATE THICKNESS : 2 MM\nPIN DIA : 5 MM\nROLLER DIA : 8 MM\nBUSH DIA : 6 MM","Mtr.",550],
["ATTACHMENT CHAIN","Attachment Chain with Extended Pins 38.10 MM Pitch","Attachment 38.10",
 "PLATE THICKNESS : 4 MM\nPIN DIA : 10 MM\nBUSH DIA : 14 MM\nROLLER DIA : 20 MM\nATTACHMENT LENGTH : 50 MM","Mtr.",1800],
["PINTLE CHAIN","Single Strand Pintle Chain 101.60 MM Pitch","Pintle 101.60",
 "PLATE THICKNESS : 8 MM\nPIN DIA : 16 MM\nBUSH DIA : 22 MM\nSIDE LINK : 45 X 8 MM","Mtr.",2950],
["CRANK CHAIN","Crank Chain for Sugar Mills 76.20 MM Pitch","Crank 76.20",
 "PLATE THICKNESS : 6 MM\nPIN DIA : 12 MM\nBUSH DIA : 16 MM\nROLLER DIA : 24 MM\nSIDE LINK : 36 X 6 MM","Mtr.",2450],
["BAGASSE CARRIER CHAIN","Bagasse Carrier Chain 152.40 MM Pitch","Bagasse Carrier 152.40",
 "PLATE THICKNESS : 8 MM\nPIN DIA : 20 MM\nBUSH DIA : 28 MM\nROLLER DIA : 40 MM\nATTACHMENT : 80 X 8 MM","Mtr.",2850],
["FORGED CHAIN","Forged Link Chain 142.25 MM Pitch","Forged 142.25",
 "PLATE THICKNESS : 12 MM\nPIN DIA : 22 MM\nBUSH DIA : 30 MM\nSIDE LINK : 55 X 12 MM","Mtr.",3600],
["SLAT CHAIN","Slat Chain for Conveyor 38.10 MM Pitch","Slat 38.10",
 "PLATE THICKNESS : 3 MM\nPIN DIA : 8 MM\nROLLER DIA : 14 MM\nSLAT WIDTH : 50 MM","Mtr.",1250],
["BUCHER CHAIN","Bucher Type Chain 100 MM Pitch","Bucher 100",
 "PLATE THICKNESS : 8 MM\nPIN DIA : 16 MM\nBUSH DIA : 22 MM\nROLLER DIA : 32 MM","Mtr.",3100],
["BUCHER HARVESTING CHAIN","Bucher Harvesting Chain 101.60 MM Pitch","Bucher Harv 101.60",
 "PLATE THICKNESS : 6 MM\nPIN DIA : 14 MM\nBUSH DIA : 20 MM\nROLLER DIA : 28 MM\nATTACHMENT : 65 X 6 MM","Mtr.",3400],
["STEEL BUSHD ROLLER CHAIN","Steel Bushed Roller Chain 152.40 MM Pitch","SB Roller 152.40",
 "PLATE THICKNESS : 10 MM\nPIN DIA : 20 MM\nBUSH DIA : 28 MM\nROLLER DIA : 42 MM\nSIDE LINK : 60 X 10 MM","Mtr.",3200],
]
for i,rd in enumerate(items,start=2):
    for j,v in enumerate(rd):
        c=ws2.cell(row=i,column=j+1,value=v)
        c.font=f(sz=10);c.alignment=a('left' if j in[1,3] else 'center',wrap=True);c.border=thin_all
        if j==5:c.number_format='#,##0.00'

ws2.auto_filter.ref=f"A1:F{1+len(items)}"
print("Item_Catalog done")

# =================================================================
# SHEET 3: CUSTOMER_LIST
# =================================================================
ws3=wb.create_sheet("Customer_List")
for col,w in {'A':28,'B':30,'C':45}.items():
    ws3.column_dimensions[col].width=w

for i,h in enumerate(["Customer_Name","Company","Address"]):
    c=ws3.cell(row=1,column=i+1,value=h)
    c.font=f(sz=10,bold=True,color="FFFFFF");c.fill=PatternFill(start_color=DARK_RED,end_color=DARK_RED,fill_type='solid')
    c.alignment=a('center');c.border=thin_all

cust=[
["The General Manager","Novel Sugar Limited","Pilibhit (U.P.)"],
["The Purchase Manager","Ranipur Sugar Mills Ltd.","Ranipur, Dist. Bijnor, U.P."],
["The Works Manager","KCSM Sugar Ltd.","Kutch, Gujarat"],
["The Director","Punjab Sugar Mills Ltd.","Moga, Punjab"],
["The General Manager","TPT Sugar Ltd.","Tiruchirapalli, Tamil Nadu"],
["The Purchase Manager","Arignar Anna Sugar Mills","Cuddalore, Tamil Nadu"],
["The Works Manager","Vellore Sugar Co. Ltd.","Vellore, Tamil Nadu"],
["The General Manager","Chengalpattu Sugar Mills","Chengalpattu, Tamil Nadu"],
["The Purchase Manager","Dharmapuri Sugar Mills","Dharmapuri, Tamil Nadu"],
]
for i,rd in enumerate(cust,start=2):
    for j,v in enumerate(rd):
        c=ws3.cell(row=i,column=j+1,value=v)
        c.font=f(sz=10);c.alignment=a('left');c.border=thin_all
ws3.auto_filter.ref=f"A1:C{1+len(cust)}"
print("Customer_List done")

# =================================================================
# SHEET 4: QUOTATION_REGISTER
# =================================================================
ws4=wb.create_sheet("Quotation_Register")
for col,w in {'A':14,'B':14,'C':28,'D':30,'E':28,'F':12,'G':14,'H':35,'I':14,'J':14}.items():
    ws4.column_dimensions[col].width=w

for i,h in enumerate(["Quotation_No","Date","Customer_Name","Company","Subject",
                       "Total_Qty","Grand_Total","Items","Status","Notes"]):
    c=ws4.cell(row=1,column=i+1,value=h)
    c.font=f(sz=10,bold=True,color="FFFFFF");c.fill=PatternFill(start_color=DARK_RED,end_color=DARK_RED,fill_type='solid')
    c.alignment=a('center',wrap=True);c.border=thin_all

reg=[
["AQ-2026-001","2026-05-26","The General Manager","Novel Sugar Limited",
 "Quotation for Sugar Elevator Chain",35,94500,"SUGAR ELEVATOR CHAIN","Sent",""],
["AQ-2026-002","2026-06-10","The Purchase Manager","Ranipur Sugar Mills Ltd.",
 "Quotation for RBC Chain",50,160000,"DOUBLE STRAND PINTLE CHAIN","Pending",""],
]
for i,rd in enumerate(reg,start=2):
    for j,v in enumerate(rd):
        c=ws4.cell(row=i,column=j+1,value=v)
        c.font=f(sz=10);c.alignment=a('left' if j<4 else 'center');c.border=thin_all
ws4.auto_filter.ref=f"A1:J{1+len(reg)}"

ws4.cell(row=10,column=1,value="LEGEND:").font=f(sz=10,bold=True)
ws4.cell(row=11,column=1,value="This sheet logs every quotation issued. After completing a quotation on the QUOTATION sheet, copy its details here.").font=f(sz=9,italic=True)
ws4.merge_cells('A11:J11')
print("Quotation_Register done")

# ── SAVE ──
out="/home/claude/xlsx_proj/Aypee_Quotation_System.xlsx"
wb.save(out)
print(f"\nSaved → {out}")
