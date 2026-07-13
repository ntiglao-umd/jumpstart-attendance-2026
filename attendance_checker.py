import cv2
import requests
import os
import time
import tkinter as tk
from tkinter import messagebox
import time
import csv

import sqlite3

with sqlite3.connect('jumpstart2026.db') as conn:
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATE DEFAULT (datetime('now','localtime')),
            student_ID TEXT NOT NULL,
            student_name TEXT NOT NULL,
            UNIQUE(student_ID, timestamp)
        )
    ''')

#cursor.execute("INSERT INTO attendance (student_ID, student_name) VALUES (?, ?)", ("12345", "John Doe"))
#conn.commit()

# Display attendance records
#cursor.execute("SELECT * FROM attendance")
#records = cursor.fetchall()
#for record in records:
#    print(record)

camera_id = 0
delay = 1
window_name = 'Attendance Checker'

def read_csv_to_dict(file_path):
    data_dict = {}
    with open(file_path, mode='r') as file:
        csv_reader = csv.DictReader(file)
        print(csv_reader)
        for row in csv_reader:
            # Assuming 'id' is unique and should be the key in the dictionary
            key = row['ID']
            data_dict[key] = row['FirstName'] + ' ' + row['LastName']
            print(row)
    return data_dict

# Path to your CSV file
file_path = 'StudentDB-2026.csv'

# Read the CSV and store data in a dictionary
data_dict = read_csv_to_dict(file_path)

# Print the dictionary
for key, value in data_dict.items():
    print(f"{key}: {value}")

qcd = cv2.QRCodeDetector()
cap = cv2.VideoCapture(camera_id)
url1 = 'https://docs.google.com/forms/d/e/1FAIpQLSd_Mc-dGWGKyVbBLGHI3PXpfIzn69iofiOowEI4XxH8QHZqUA/viewform'
url2 = 'https://docs.google.com/forms/d/e/1FAIpQLSd_Mc-dGWGKyVbBLGHI3PXpfIzn69iofiOowEI4XxH8QHZqUA/formResponse'

message = ""
message_start_time = None
message_duration = 2  # 3 seconds

while True:
    ret, frame = cap.read()

    if ret:
        ret_qr, decoded_info, points, _ = qcd.detectAndDecodeMulti(frame)
        if ret_qr:
            for s, p in zip(decoded_info, points):
                if s:
                    
                    # Set message and start time
                    message = f"ID {s}: {data_dict[s]} marked as present"
                    message_start_time = time.time()

                    #print(response)
                    #string = "entry.xxx=Option+1&entry.xxx=option2"
                    #r = requests.post(url, params = string)
                    color = (0, 255, 0)

                    # Save attendance to database
                    #cursor.execute("INSERT INTO attendance (student_ID, student_name) VALUES (?, ?)", (s, data_dict[s]))
                    #conn.commit()

                    # Check if the student has checked in within the last hour (in local time)
                    cursor.execute("SELECT 1 FROM attendance WHERE student_ID = ? AND timestamp > datetime('now', '-5 minutes', 'localtime')", 
    (s,))
                    recent_attendance = cursor.fetchone()
                    if not recent_attendance:
                        try:
                            cursor.execute("INSERT INTO attendance (student_ID, student_name) VALUES (?, ?)", (s, data_dict[s]))
                            print(f"Attendance for {s} recorded.")
                            conn.commit()
                            print(s)
                            session = requests.Session()
                            session.get(url1)
                            form_data = {"entry.2093000039":s + " " + data_dict[s]}
                            response = session.post(url2, data=form_data)
                        except sqlite3.IntegrityError:
                            print(f"Duplicate attendance for {s} within the hour prevented.")
                    else:
                        print(f"{s} Already checked in within the last 5 minutes. No new record added.")
                else:
                    color = (0, 0, 255)
                frame = cv2.polylines(frame, [p.astype(int)], True, color, 8)
        
        # Display the message if it is set and within the duration
        if message and (time.time() - message_start_time < message_duration):
            cv2.putText(frame, message, (50, 80), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 255, 0), 5, cv2.LINE_AA)
        else:
            message = ""  # Clear message after duration

        cv2.imshow(window_name, frame)

        # Show attendance records
        #cursor.execute("SELECT * FROM attendance")
        #records = cursor.fetchall()
        #for record in records:
        #    print(record)

    if cv2.waitKey(delay) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
cv2.destroyWindow(window_name)





