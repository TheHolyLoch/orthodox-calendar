#!/usr/bin/env python
# coding: utf-8

## Example URL for 3rd Sunday of Pascha:
# https://www.holytrinityorthodox.com/calendar/calendar2.php?month=04&today=26&year=2026&dt=1&header=1&lives=1&trp=0&scripture=0

# In[60]:


from bs4 import BeautifulSoup
import requests
import pandas as pd
from datetime import datetime, timedelta
import sqlite3
import warnings
from tqdm import tqdm
warnings.filterwarnings('ignore')


# In[71]:


year = 2026
days = 365

start_date = datetime(year, 1, 1)  # Year, Month, Day
dates = []
month = []
dates_2 = []
fast_feasts = []
liturgical_days = []
liturgical_days2 = []
saints = []
for i in tqdm(range(days)):
    current_date = start_date + timedelta(days=i)
    
    # Extract day, month, and year
    day_number = current_date.strftime("%d")
    month_number = current_date.strftime("%m")
    
    url = f"https://www.holytrinityorthodox.com/calendar/calendar2.php?month={month_number}&today={day_number}&year={year}&dt=1&header=1&lives=1&trp=0&scripture=0"
    
    page = requests.get(url)
    soup = BeautifulSoup(page.content, "html.parser")
    
    temp = soup.find('span', class_='dataheader').text.split('/')
    date = " ".join(temp[0].split()[1:]).strip()
    date2 = temp[1].strip()
    try:
        fast_feast = soup.find('span', class_='headerfast').text.strip()
    except:
        fast_feast = ''
    liturgical_day = " ".join(soup.find('span', class_='headerheader').text.split()[:2])
    try:
        liturgical_day2 = soup.find('i').text.split()[0]
    except:
        liturgical_day2 = ''
    if liturgical_day2 not in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]:
        liturgical_day2 = ''
    saint = []
    rows = soup.find_all('span', class_='minortext')
    for row in rows:
        if 'British' in row.text or 'Celtic' in row.text:
            saint.append(row.text) 
    saint = " | ".join(saint)
    
    # add everything
    # dates.append(date)
    dates.append(day_number)
    month.append(month_number)
    dates_2.append(date2)
    fast_feasts.append(fast_feast)
    liturgical_days.append(liturgical_day)
    liturgical_days2.append(liturgical_day2)
    saints.append(saint)


# In[72]:


df = pd.DataFrame()
df['DATE (Gregorian)'] = dates
df['MONTH'] = month
df['DATE_2 (Julian)'] = dates_2
df['LITURGICAL_DAY'] = liturgical_days
df['FAST_FEAST'] = fast_feasts
df['LITURGICAL_DAY_2'] = liturgical_days2
df['SAINTS'] = saints

# storing table in db
con = sqlite3.connect("2026.db")
df.to_sql('table', con, if_exists='append', index=False)
con.close()


# In[ ]:




