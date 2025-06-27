from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import csv
import os
from collections import defaultdict
import pandas as pd
from io import BytesIO
import uuid
from charset_normalizer import detect

app = Flask(__name__)
CORS(app)  # 🔹 Enable CORS

# 🔹 Increase file size limit to 50 MB
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def detect_encoding(file_path):
    try:
        with open(file_path, 'rb') as f:
            result = detect(f.read())
            encoding = result.get('encoding', 'utf-8')
            confidence = result.get('confidence', 0)
            if confidence < 0.8:
                encoding = 'latin-1'
            print(f"Detected encoding for {file_path}: {encoding} (confidence: {confidence})")
            return encoding
    except Exception as e:
        print(f"Error detecting encoding for {file_path}: {str(e)}")
        raise

def find_channel_column(headers):
    possible_names = ['Channel', 'channel', 'CHANNEL', 'Channel_Name', 'channel_name']
    for name in possible_names:
        if name in headers:
            return name
    return None

@app.route('/health')
def health_check():
    return jsonify({'status': 'success', 'message': 'Server is running'})

@app.route('/')
def index():
    return send_file('index.html')

@app.route('/upload', methods=['POST'])
def upload_files():
    try:
        files = request.files.getlist('files')
        saved_files = []
        channels = set()
        failed_files = []

        for file in files:
            if not file or not file.filename:
                failed_files.append({'filename': 'Unknown', 'error': 'Empty or missing file'})
                continue

            if not file.filename.lower().endswith('.csv'):
                failed_files.append({'filename': file.filename, 'error': 'Only CSV files are allowed'})
                continue

            file_path = os.path.join(UPLOAD_FOLDER, str(uuid.uuid4()) + '.csv')
            file.save(file_path)

            try:
                encoding = detect_encoding(file_path)
                with open(file_path, newline='', encoding=encoding, errors='replace') as f:
                    reader = csv.DictReader(f)
                    headers = reader.fieldnames
                    if not headers:
                        failed_files.append({'filename': file.filename, 'error': 'No headers found'})
                        os.remove(file_path)
                        continue

                    channel_col = find_channel_column(headers)
                    if not channel_col:
                        failed_files.append({'filename': file.filename, 'error': 'No "Channel" column found'})
                        os.remove(file_path)
                        continue

                    for row in reader:
                        channel = row.get(channel_col, '').strip().lower()
                        if channel:
                            channels.add(channel)

                    if not channels:
                        failed_files.append({'filename': file.filename, 'error': 'No valid "Channel" values found'})
                        os.remove(file_path)
                        continue

                    saved_files.append(file_path)

            except Exception as e:
                failed_files.append({'filename': file.filename, 'error': f'Error: {str(e)}'})
                if os.path.exists(file_path):
                    os.remove(file_path)

        return jsonify({
            'status': 'success' if saved_files else 'error',
            'channels': sorted(list(channels)),
            'files': saved_files,
            'failed_files': failed_files
        })

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Unexpected error: {str(e)}'
        }), 500

@app.route('/generate_report', methods=['POST'])
def generate_report():
    try:
        data = request.get_json()
        selected_channels = [ch.lower() for ch in data.get('channels', [])]
        file_paths = data.get('files', [])
        failed_files = []
        report_data = defaultdict(lambda: defaultdict(float))
        all_payment_months = set()

        for file_path in file_paths:
            try:
                encoding = detect_encoding(file_path)
                with open(file_path, newline='', encoding=encoding, errors='replace') as f:
                    reader = csv.DictReader(f)
                    headers = reader.fieldnames
                    channel_col = find_channel_column(headers)
                    if not channel_col:
                        failed_files.append({'filename': os.path.basename(file_path), 'error': 'Missing "Channel" column'})
                        continue

                    for row in reader:
                        channel = row.get(channel_col, '').strip().lower()
                        if selected_channels and channel not in selected_channels:
                            continue

                        act_month = row.get('Act_Month', '').split('.')[0].zfill(6)

                        for i in range(1, 5):
                            inv_amt = row.get(f'InvAmt_M{i}', '').strip()
                            pay_date = row.get(f'PaymentDt_Inv{i}', '').strip()
                            if not inv_amt or not pay_date:
                                continue

                            try:
                                amt = float(inv_amt)
                                if amt == 0:
                                    continue
                                parts = pay_date.split('-')
                                if len(parts) < 2:
                                    continue
                                payment_month = parts[0] + parts[1].zfill(2)
                                all_payment_months.add(payment_month)
                                report_data[act_month][payment_month] += amt
                            except:
                                continue
            except Exception as e:
                failed_files.append({'filename': os.path.basename(file_path), 'error': str(e)})

        sorted_months = sorted(all_payment_months)
        final_rows = []
        for act_month in sorted(report_data.keys()):
            row = {'Act_Month': act_month}
            total = 0.0
            for pm in sorted_months:
                amt = report_data[act_month].get(pm, 0.0)
                row[pm] = round(amt, 2)
                total += amt
            row['Total'] = round(total, 2)
            final_rows.append(row)

        df = pd.DataFrame(final_rows)
        if not df.empty:
            df = df[['Act_Month'] + sorted_months + ['Total']]

        return jsonify({
            'status': 'success' if not df.empty else 'no_data',
            'report': final_rows,
            'columns': ['Act_Month'] + sorted_months + ['Total'],
            'failed_files': failed_files
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/download/<file_type>', methods=['POST'])
def download(file_type):
    try:
        data = request.get_json()
        report = data.get('report', [])
        columns = data.get('columns', [])
        channels = data.get('channels', ['report'])
        channel_name = '_'.join(channels) if channels else 'report'
        df = pd.DataFrame(report, columns=columns)

        output = BytesIO()
        if file_type == 'csv':
            df.to_csv(output, index=False)
            mime = 'text/csv'
            filename = f'payment_summary_{channel_name}.csv'
        elif file_type == 'excel':
            df.to_excel(output, index=False, engine='openpyxl')
            mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            filename = f'payment_summary_{channel_name}.xlsx'
        else:
            return jsonify({'status': 'error', 'message': 'Invalid file type'}), 400

        output.seek(0)
        return send_file(output, mimetype=mime, as_attachment=True, download_name=filename)

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/cleanup', methods=['POST'])
def cleanup():
    try:
        data = request.get_json()
        file_paths = data.get('files', [])
        for path in file_paths:
            if os.path.exists(path):
                os.remove(path)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
