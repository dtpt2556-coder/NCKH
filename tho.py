from vnstock import Vnstock
import pandas as pd
import time

# 1. Danh sách đầy đủ các mã cổ phiếu phi tài chính của bạn
list_tickers = ['ACC', 'ACG', 'ASG', 'AST', 'BCG', 'BKG', 'BSR', 'CCC', 'CII', 'CLL', 'CRC', 'CTC', 'CTI', 'CVT', 'DGW', 'DHC', 'DLG', 'DPG', 'DQC', 'DTL', 'DVP', 'DXV', 'FCM', 'FCN', 'GEE', 'GEL', 'GEX', 'GMD', 'GMH', 'GSP', 'GTA', 'HAH', 'HAP', 'HAS', 'HHP', 'HHS', 'HHV', 'HMC', 'HPG', 'HSG', 'HTI', 'HTL', 'HTN', 'HTV', 'HUB', 'HVH', 'HVN', 'ILB', 'JVC', 'LBM', 'LCG', 'LGC', 'MCP', 'MHC', 'NCT', 'NHH', 'NHT', 'NKG', 'PAC', 'PDN', 'PDV', 'PET', 'PHC', 'PIT', 'PJT', 'PLX', 'PMG', 'PTB', 'PTC', 'PVP', 'PVT', 'QNP', 'RAL', 'REE', 'SAM', 'SBG', 'SBV', 'SCS', 'SFI', 'SGN', 'SHI', 'SKG', 'SMA', 'SMC', 'SRF', 'STG', 'SVT', 'TCD', 'TCL', 'TCO', 'TCR', 'TCT', 'TDG', 'TEG', 'THG', 'TLD', 'TLG', 'TLH', 'TMS', 'TMT', 'TNI', 'TSA', 'TSC', 'TYA', 'VCA', 'VCG', 'VFG', 'VGC', 'VID', 'VIP', 'VJC', 'VMD', 'VNE', 'VNL', 'VNS', 'VOS', 'VPG', 'VSC', 'VSI', 'VTB', 'VTO', 'VTP', 'VVS', 'YBM', 'CMG', 'CTR', 'ELC', 'FPT', 'ICT', 'ITD', 'SGT', 'ADG', 'BTT', 'CCI', 'CMV', 'COM', 'CTF', 'DAH', 'DSN', 'FRT', 'HAX', 'MWG', 'NVT', 'PNC', 'PNJ', 'SFC', 'SVC', 'VNG', 'VPL', 'YEG', 'AAM', 'AAN', 'AAT', 'ABT', 'ACL', 'ADS', 'AFX', 'ANT', 'ANV', 'ASM', 'BAF', 'BHN', 'BRC', 'CCL', 'CMX', 'CSM', 'DAT', 'DBC', 'DRC', 'EVE', 'FMC', 'GDT', 'GIL', 'HAG', 'HPA', 'HSL', 'HTG', 'IDI', 'KDC', 'KMR', 'LAF', 'LSS', 'MCH', 'MCM', 'MSH', 'MSN', 'NAF', 'NAV', 'NSC', 'PAN', 'RYG', 'SAB', 'SAV', 'SBT', 'SHA', 'SMB', 'SRC', 'SSC', 'STK', 'SVD', 'TCM', 'TTF', 'TVT', 'VCF', 'VHC', 'VNM', 'ASP', 'BTP', 'BWE', 'CHP', 'CLW', 'CNG', 'DRL', 'GAS', 'GEG', 'GHC', 'HID', 'HNA', 'KHP', 'PGC', 'PGD', 'PGV', 'POW', 'PPC', 'SBA', 'SHP', 'SIP', 'SJD', 'TBC', 'TDC', 'TDM', 'TDW', 'TMP', 'TTA', 'TTE', 'UIC', 'VPD', 'VSH', 'AAA', 'AAD', 'APH', 'BFC', 'BMC', 'BMP', 'CSV', 'DCM', 'DGC', 'DHA', 'DHM', 'DPM', 'DPR', 'DTT', 'GVR', 'HCD', 'HII', 'HRC', 'KSB', 'LIX', 'MDG', 'NNC', 'PHR', 'PLP', 'PVD', 'SFG', 'TDP', 'TNC', 'TNT', 'TPC', 'TRC', 'VPS', 'DBD', 'DBT', 'DCL', 'DHG', 'DMC', 'FIT', 'IMP', 'OPC', 'SPM', 'TNH', 'TRA', 'VDP']

# 2. Khởi tạo công cụ tải dữ liệu
stock = Vnstock()
all_results = []
count = 0

print(f"Bắt đầu tải dữ liệu lịch sử cho {len(list_tickers)} mã cổ phiếu...")

for ticker in list_tickers:
    try:
        # Tải dữ liệu từ 2018 đến 2026
        df_hist = stock.stock(symbol=ticker, source='VCI').quote.history(start='2018-01-01', end='2026-06-01')
        
        if df_hist is not None and not df_hist.empty:
            df_hist['time'] = pd.to_datetime(df_hist['time'])
            df_hist['year'] = df_hist['time'].dt.year
            
            # Lấy dòng cuối cùng của mỗi năm (phiên chốt năm)
            df_last_of_year = df_hist.groupby('year').last().reset_index()
            
            # Chỉ lọc lấy giai đoạn từ 2018 đến 2025
            df_last_of_year = df_last_of_year[(df_last_of_year['year'] >= 2018) & (df_last_of_year['year'] <= 2025)]
            
            # Lưu lại cột năm và giá đóng cửa
            df_filtered = df_last_of_year[['year', 'close']].copy()
            df_filtered['Mã CP'] = ticker
            
            all_results.append(df_filtered)
            
        count += 1
        if count % 20 == 0:
            print(f" -> Đã quét xong {count}/{len(list_tickers)} mã...")
            
        time.sleep(0.1) # Tạm nghỉ ngắn để không bị lỗi nghẽn mạng
        
    except Exception as e:
        continue

# 3. Tổng hợp dữ liệu và tiến hành xuất file Excel ra ổ C
if all_results:
    df_final = pd.concat(all_results, ignore_index=True)
    
    # Xoay bảng dữ liệu (Dòng là mã CP, Cột là các năm)
    df_pivot = df_final.pivot(index='Mã CP', columns='year', values='close')
    df_pivot.index.name = 'Mã CP'
    
    print("\n✅ TẢI DỮ LIỆU THÀNH CÔNG! Đang tạo file Excel...")
    
    # --- ĐƯỜNG DẪN LƯU VÀO Ổ C ---
    duong_dan_excel = r'C:\Users\PC\Gia_Chot_Nam_Phi_Tai_Chinh.xlsx'
    
    # Câu lệnh chính để xuất ra Excel
    df_pivot.to_excel(duong_dan_excel)
    
    print(f"\n📁 File của bạn đã được xuất thành công vào ổ C tại:")
    print(f"👉 {duong_dan_excel}")
else:
    print("\n❌ Thất bại: Không lấy được dữ liệu. Kiểm tra lại kết nối mạng.")