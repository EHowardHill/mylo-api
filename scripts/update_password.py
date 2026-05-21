import argparse
import pymongo
from werkzeug.security import generate_password_hash
from getpass import getpass
import sys

# Configuration (Matches your shared.py)
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "mylo"
COLLECTION_NAME = "emails"  # Based on the db["emails"] usage in shared.py

def get_db_collection():
    try:
        client = pymongo.MongoClient(MONGO_URI)
        db = client[DB_NAME]
        return db[COLLECTION_NAME]
    except Exception as e:
        print(f"Error connecting to database: {e}")
        sys.exit(1)

def update_user_password(identifier, is_handle=False):
    collection = get_db_collection()
    
    # Determine search criteria
    query = {"user_handle": identifier} if is_handle else {"email": identifier.lower()}
    
    # Check if user exists
    user = collection.find_one(query)
    
    if not user:
        print(f"❌ Error: User with {'handle' if is_handle else 'email'} '{identifier}' not found.")
        return

    print(f"✓ Found user: {user.get('user_full_name', 'Unknown Name')} ({user.get('email')})")
    
    # Securely prompt for new password
    while True:
        new_password = getpass("Enter new password: ")
        confirm_password = getpass("Confirm new password: ")
        
        if new_password == confirm_password:
            if len(new_password) > 0:
                break
            print("Password cannot be empty.")
        else:
            print("Passwords do not match. Please try again.")

    # Hash and update
    try:
        hashed_password = generate_password_hash(new_password)
        
        result = collection.update_one(
            {"_id": user["_id"]},
            {"$set": {"password": hashed_password}}
        )
        
        if result.modified_count > 0:
            print(f"✅ Password successfully updated for {identifier}.")
        else:
            print("⚠️  Password was identical to the existing one; no changes made.")
            
    except Exception as e:
        print(f"❌ An error occurred during update: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manually update a user's password in the MongoDB database.")
    
    # Create a mutually exclusive group so you can search by email OR handle, but not both
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--email", help="The email address of the user")
    group.add_argument("--handle", help="The user handle of the user")
    
    args = parser.parse_args()

    if args.email:
        update_user_password(args.email, is_handle=False)
    elif args.handle:
        update_user_password(args.handle, is_handle=True)