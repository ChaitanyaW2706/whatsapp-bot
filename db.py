import mysql.connector
from config import DB_CONFIG
import datetime

def get_db():
    return mysql.connector.connect(**DB_CONFIG)

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)
def get_car_image_base64_by_model(model_name):
    """
    Fetch car image from database by model name
    Returns base64 string or None
    """
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)

        # Try the column name you mentioned in sales.py
        cursor.execute(
            "SELECT car_image_base64 FROM sales_car_details WHERE model = %s LIMIT 1",
            (model_name,)
        )

        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row and row["car_image_base64"]:
            print(f"✅ Found image for {model_name} in database")
            return row["car_image_base64"]
        
        print(f"⚠️ No image found for {model_name} in database")
        return None
    except Exception as e:
        print(f"❌ Error fetching car image from DB: {e}")
        return None


def get_car_brochure_base64_by_model(model_name):
    """
    Fetch car brochure PDF from database by model name
    Returns base64 string or None
    """
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)

        # Try the column name for brochure
        cursor.execute(
            "SELECT brochure_pdf_base64 FROM sales_car_details WHERE model = %s LIMIT 1",
            (model_name,)
        )

        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row and row["brochure_pdf_base64"]:
            print(f"✅ Found PDF brochure for {model_name} in database")
            return row["brochure_pdf_base64"]
        
        print(f"⚠️ No PDF brochure found for {model_name} in database")
        return None
    except Exception as e:
        print(f"❌ Error fetching car brochure from DB: {e}")
        return None


def get_cars_by_type(car_type):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT id, make, model, type, fuel_type, transmission_type,
               `Ex-Showroom Price Base Model` AS ex_showroom_price,
               `Ex-Showroom Price Top Model` AS on_road_price,
               car_image_base64
        FROM sales_car_details
        WHERE type = %s
        ORDER BY `Ex-Showroom Price Top Model` DESC
    """
    cursor.execute(query, (car_type,))
    cars = cursor.fetchall()

    cursor.close()
    conn.close()
    return cars


def get_car_by_id(car_id):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT id, make, model, variant, mileage_kmph, type_id,
               car_image_base64, brochure_pdf_base64,
               `Ex-Showroom Price Base Model` AS ex_showroom_price,
               `Ex-Showroom Price Top Model` AS on_road_price
        FROM sales_car_details WHERE id = %s
    """, (car_id,))
    car = cursor.fetchone()

    cursor.close()
    conn.close()
    return car


def get_available_car_types():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT id, type_name
        FROM car_types
        ORDER BY type_name
    """

    cursor.execute(query)
    result = cursor.fetchall()

    cursor.close()
    conn.close()

    print("🔎 Car Types from DB:", result)  # debug

    return result


def get_cars_by_type_id(type_id):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT id, make, model, variant, mileage_kmph, type_id,
               car_image_base64, brochure_pdf_base64,
               `Ex-Showroom Price Base Model` AS ex_showroom_price,
               `Ex-Showroom Price Top Model` AS on_road_price
        FROM sales_car_details
        WHERE type_id = %s
        ORDER BY `Ex-Showroom Price Base Model` DESC
    """

    cursor.execute(query, (type_id,))
    result = cursor.fetchall()

    cursor.close()
    conn.close()

    return result


def get_full_car_details(car_id):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    # 1️⃣ Get main car details
    cursor.execute("""
        SELECT scd.*, 
               scd.`Ex-Showroom Price Base Model` AS ex_showroom_price,
               scd.`Ex-Showroom Price Top Model` AS on_road_price,
               ct.type_name
        FROM sales_car_details scd
        LEFT JOIN car_types ct
            ON scd.type_id = ct.id
        WHERE scd.id = %s
    """, (car_id,))

    car = cursor.fetchone()

    if not car:
        cursor.close()
        conn.close()
        return None

    # 2️⃣ Get colors
    cursor.execute("""
        SELECT color_name
        FROM car_colors
        WHERE car_id = %s
    """, (car_id,))
    colors = [row['color_name'] for row in cursor.fetchall()]

    # 3️⃣ Get fuel types
    cursor.execute("""
        SELECT fuel_type
        FROM car_fuel_types
        WHERE car_id = %s
    """, (car_id,))
    fuel_types = [row['fuel_type'] for row in cursor.fetchall()]

    # 4️⃣ Get transmissions
    cursor.execute("""
        SELECT transmission_type
        FROM car_transmissions
        WHERE car_id = %s
    """, (car_id,))
    transmissions = [row['transmission_type'] for row in cursor.fetchall()]

    cursor.close()
    conn.close()

    return {
        "car": car,
        "colors": colors,
        "fuel_types": fuel_types,
        "transmissions": transmissions
    }


def get_used_cars_by_budget(min_price, max_price):
    conn = mysql.connector.connect(**DB_CONFIG)
    cur = conn.cursor(dictionary=True)

    cur.execute("""
        SELECT serial_number, make, model, manufacturing_year, fuel_type,
               mileage_km, estimated_selling_price, image_url
        FROM carstockdata
        WHERE LOWER(ready_for_sales) = 'available'
          AND estimated_selling_price BETWEEN %s AND %s
        ORDER BY estimated_selling_price ASC
        LIMIT 5
    """, (min_price, max_price))

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def get_car_types_by_budget(min_price, max_price):
    conn = get_db_connection()
    cur = conn.cursor()
    query = """
        SELECT DISTINCT `type`
        FROM carstockdata
        WHERE LOWER(ready_for_sales) = 'available'
          AND estimated_selling_price BETWEEN %s AND %s
          AND `type` IS NOT NULL
          AND TRIM(`type`) != ''
        ORDER BY `type`
    """
    cur.execute(query, (min_price, max_price))
    types = [row[0] for row in cur.fetchall() if row[0]]
    cur.close()
    conn.close()
    return types

# Add this to db.py

def get_latest_manufacturing_year(car_id):
    """Get the latest manufacturing year for a specific car"""
    import mysql.connector
    from config import DB_CONFIG
    
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT manufacturing_year 
            FROM sales_car_manufacturing_years 
            WHERE car_id = %s 
            ORDER BY manufacturing_year DESC 
            LIMIT 1
        """, (car_id,))
        
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if result:
            return result[0]
        return None
    except Exception as e:
        print(f"❌ Error fetching manufacturing year: {e}")
        return None
    
# Add this to db.py

def get_all_cars():
    """Get all cars from sales_car_details table"""
    import mysql.connector
    from config import DB_CONFIG
    
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("""
            SELECT id, make, model 
            FROM sales_car_details 
            ORDER BY `Ex-Showroom Price Base Model` DESC
        """)
        
        cars = cursor.fetchall()
        cursor.close()
        conn.close()
        
        print(f"✅ Found {len(cars)} cars in database")
        return cars
    except Exception as e:
        print(f"❌ Error fetching all cars: {e}")
        return []
    
# Add this to db.py

def get_all_cars_paginated(page=1, per_page=8):
    """Get all cars from sales_car_details table with pagination"""
    import mysql.connector
    from config import DB_CONFIG
    
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        
        # Get total count
        cursor.execute("SELECT COUNT(*) as total FROM sales_car_details")
        total = cursor.fetchone()["total"]
        
        # Calculate offset
        offset = (page - 1) * per_page
        total_pages = (total + per_page - 1) // per_page
        
        # Get paginated results
        cursor.execute("""
            SELECT id, make, model 
            FROM sales_car_details 
            ORDER BY `Ex-Showroom Price Base Model` DESC
            LIMIT %s OFFSET %s
        """, (per_page, offset))
        
        cars = cursor.fetchall()
        cursor.close()
        conn.close()
        
        print(f"✅ Found {len(cars)} cars in database (page {page} of {total_pages})")
        
        return {
            "cars": cars,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1
        }
    except Exception as e:
        print(f"❌ Error fetching cars: {e}")
        return {
            "cars": [],
            "total": 0,
            "page": page,
            "per_page": per_page,
            "total_pages": 0,
            "has_next": False,
            "has_prev": False
        }

# =========================
# USER ACTIVITY LOGGING
# =========================
def log_user_activity(phone, action_type):
    """
    Log which section the user visits.
    action_type: 'used_car', 'valuation', 'contact_us', 'about_us', 'start', 'other'
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_activity_log (contact_number, action_type)
            VALUES (%s, %s)
        """, (str(phone)[-10:], action_type))
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ Activity logged: {phone} → {action_type}")
    except Exception as e:
        print(f"❌ Failed to log user activity: {e}")


# =========================
# NOTIFY REQUEST (Brand not available)
# =========================
def save_notify_request(phone, budget, brand_requested, car_type, customer_name=None):
    """
    Save a 'notify me when available' request when a user's desired brand
    is not currently in stock.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO notify_requests
                (price_range, brand_requested, car_type, customer_name, phone_number, created_on)
            VALUES (%s, %s, %s, %s, %s, NOW())
        """, (budget, brand_requested, car_type, customer_name, str(phone)[-10:]))
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ Notify request saved: {phone} wants {brand_requested} in {budget}")
    except Exception as e:
        print(f"❌ Failed to save notify request: {e}")


# =========================
# TABLE CREATION — run once at app startup
# =========================
def create_tracking_tables():
    """
    Creates user_activity_log and notify_requests tables if they don't exist.
    Call once at app startup: from db import create_tracking_tables; create_tracking_tables()
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # ── 1. User Activity Log ─────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_activity_log (
                id             INT AUTO_INCREMENT PRIMARY KEY,
                contact_number VARCHAR(20)  NOT NULL,
                action_type    ENUM('used_car', 'valuation', 'contact_us', 'about_us', 'start', 'other') NOT NULL,
                timestamp      DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("✅ Table ready: user_activity_log")

        # ── 2. Notify Requests ───────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notify_requests (
                id               INT AUTO_INCREMENT PRIMARY KEY,
                price_range      VARCHAR(50),
                brand_requested  VARCHAR(100),
                car_type         VARCHAR(100),
                customer_name    VARCHAR(100),
                phone_number     VARCHAR(20),
                created_on       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("✅ Table ready: notify_requests")

        conn.commit()
        cursor.close()
        conn.close()

    except Exception as e:
        print(f"❌ Error creating tracking tables: {e}")


# Add these functions to your db.py file (after existing functions)

def get_vehicle_from_db(reg_no):
    """
    Fetch vehicle details from database by registration number
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT * FROM vehicle WHERE vehicleRegNo = %s LIMIT 1",
            (reg_no,)
        )

        vehicle = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if vehicle:
            print(f"✅ Vehicle found: {reg_no}")
        else:
            print(f"❌ Vehicle not found: {reg_no}")
            
        return vehicle
    except Exception as e:
        print(f"❌ Error fetching vehicle from DB: {e}")
        return None


def get_service_history(reg_no):
    """
    Fetch service history from database by registration number
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT billDate, billAmt, serviceCategory, workshopName
            FROM robillscube
            WHERE vehicleRegNo = %s
            ORDER BY billDate DESC
            LIMIT 5
        """, (reg_no,))

        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        print(f"✅ Found {len(rows)} service records for {reg_no}")
        return rows
    except Exception as e:
        print(f"❌ Error fetching service history: {e}")
        return []


def save_appointment(data):
    """
    Save appointment booking to database
    data = {
        "phone": phone_number,
        "name": customer_name,
        "reg": vehicle_reg,
        "date": appointment_date,
        "time": appointment_time
    }
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO appointment_bookings
            (phone_number, full_name, vehicle_reg,
             appointment_date, timing, booking_timestamp, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            data["phone"],
            data["name"],
            data["reg"],
            data["date"],
            data["time"],
            datetime.now(),
            'pending'
        ))

        conn.commit()
        appointment_id = cursor.lastrowid
        cursor.close()
        conn.close()
        
        print(f"✅ Appointment saved with ID: {appointment_id}")
        return appointment_id
    except Exception as e:
        print(f"❌ Error saving appointment: {e}")
        return None


def create_appointment_table():
    """
    Create appointment_bookings table if it doesn't exist
    Run this once when setting up the database
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS appointment_bookings (
                id INT AUTO_INCREMENT PRIMARY KEY,
                phone_number VARCHAR(20),
                full_name VARCHAR(100),
                vehicle_reg VARCHAR(20),
                appointment_date VARCHAR(50),
                timing VARCHAR(50),
                booking_timestamp DATETIME,
                status VARCHAR(20) DEFAULT 'pending',
                notes TEXT,
                INDEX idx_phone (phone_number),
                INDEX idx_status (status)
            )
        """)
        
        conn.commit()
        cursor.close()
        conn.close()
        print("✅ Appointment bookings table ready")
        return True
    except Exception as e:
        print(f"❌ Error creating appointment table: {e}")
        return False