import streamlit as st
import pandas as pd
import requests
import json
import time
from io import BytesIO
import base64

# Page configuration
st.set_page_config(
    page_title="Click2mail Address Validation Tool",
    page_icon="🏠",
    layout="wide"
)

def geocode_address(address_text, api_key):
    url = f"https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": address_text,
        "key": api_key
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    if not data['results']:
        raise ValueError("No results found for address")
    # Grab the first result
    result = data['results'][0]
    return result

def build_validation_request(geocode_result, original_input):
    address_components = geocode_result.get('address_components', [])
    formatted_address = geocode_result.get('formatted_address', "")

    components = {}
    poi_names = []
    for comp in address_components:
        types = comp['types']
        if any(t in types for t in ["point_of_interest", "establishment", "university"]):
            if comp['long_name'] not in poi_names:
                poi_names.append(comp['long_name'])
        elif "locality" in types:
            components["locality"] = comp['long_name']
        elif "administrative_area_level_1" in types:
            components["administrativeArea"] = comp['short_name']
        elif "country" in types:
            components["regionCode"] = comp['short_name']
        elif "postal_code" in types:
            components["postalCode"] = comp['long_name']

    # Build smart address lines
    address_lines = []
    for name in poi_names:
        address_lines.append(name)

    # Always keep the original input to ensure important names like "Harvard"
    if original_input not in address_lines:
        address_lines.insert(0, original_input)
    
    # Add the formatted address last
    if formatted_address not in address_lines:
        address_lines.append(formatted_address)

    components.setdefault("regionCode", "US")
    components["addressLines"] = address_lines

    return {
        "address": components
    }

def validate_address_google(address, api_key, enable_cass=True, region_code="US"):
    """
    Click2mail Address Validation Tool
    """

    geocode_result = geocode_address(address, api_key)
    validation_request = build_validation_request(geocode_result, address)


    url = "https://addressvalidation.googleapis.com/v1:validateAddress"
    
    headers = {
        "Content-Type": "application/json"
    }
    
    payload = {
        "address": {
            "addressLines": [address]
        },
        "enableUspsCass": enable_cass
    }
    
    params = {
        "key": api_key
    }
    
    try:
        response = requests.post(url, headers=headers, json=validation_request, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}

def _map_dpv_confirmation(code):
    """Maps DPV confirmation codes to human-readable descriptions."""
    mapping = {
        'Y': 'Address confirmed (Primary and secondary if present).',
        'N': 'Address not confirmed (No primary or secondary match).',
        'D': 'Primary confirmed, but secondary information is missing.',
        'S': 'Primary confirmed, secondary information exists but was not provided in the input.',
        'C': 'Address confirmed, but it is a Commercial Mail Receiving Agency (CMRA).',
        'B': 'Primary confirmed, but it is a PO Box or equivalent.'
    }
    return mapping.get(code, "Unknown DPV Confirmation Code")

def parse_validation_result(result, original_address, region_code="US", enable_usps_cass=True):
    """
    Parse Google Address Validation API result
    """
    if "error" in result:
        return {
            "original_address": original_address,
            "validated_address": original_address,
            "is_valid": False,
            "validation_status": f"API Error: {result['error']}",
            "lat": None,
            "lng": None,
            "usps_data": None
        }
    
    try:
        # Extract validation result
        validation_result = result.get("result", {})
        verdict = validation_result.get("verdict", {})
        address_obj = validation_result.get("address", {})

        validation_granularity = verdict.get("validationGranularity", "UNKNOWN")
        address_complete = verdict.get("addressComplete", False)
        has_inferred = verdict.get("hasInferredComponents", False)

        if validation_granularity == "SUB_PREMISE":
            status = "Highly Mailable Address (Validated to sub-unit)"
            is_valid = True
        elif validation_granularity == "PREMISE":
            status = "Standard Mailable Address (Validated to building)"
            is_valid = True
        elif validation_granularity == "STREET":
            status = "Partial Address (Street-level only, may not be reliably mailable)"
            is_valid = True
        elif validation_granularity == "LOCALITY":
            status = "Non-Mailable Address (Only city-level validated)"
            is_valid = False
        elif validation_granularity == "REGION":
            status = "Non-Mailable Address (Only region/state validated)"
            is_valid = False
        elif validation_granularity == "COUNTRY":
            status = "Non-Mailable Address (Only country validated)"
            is_valid = False
        elif validation_granularity == "OTHER":
            status = "Non-Mailable Address (Unknown or unvalidated)"
            is_valid = False
        else:
            status = "Non-Mailable Address (Unknown validation granularity)"
            is_valid = False

        # Add inferred note if relevant
        if has_inferred and validation_granularity in {"SUB_PREMISE", "PREMISE", "STREET"}:
            status += " — Note: Some components were inferred."
        
        # Get formatted address
        formatted_address = address_obj.get("formattedAddress", original_address)
        
        # Get geocoding info
        geocode = validation_result.get("geocode", {})
        location = geocode.get("location", {})
        lat = location.get("lat")
        lng = location.get("lng")
        
        
       
        
        # Get USPS data if available
        usps_data = validation_result.get("uspsData", {})
        metadata = validation_result.get("metadata", {})
        
        is_po_box = metadata.get('poBox', False) if region_code == "US" else False
        is_dpv_confirmed = (usps_data.get('dpvConfirmation') == 'Y') if region_code == "US" else False

        # Define 'is_confirmed' based on high validation quality and no user intervention needed
        is_confirmed = False
        dpv_confirmation = usps_data.get("dpvConfirmation", "N/A")
        dpv_confirmation_description = _map_dpv_confirmation(dpv_confirmation)
        is_confirmed = (
            verdict.get('validationGranularity') in ["PREMISE", "SUB_PREMISE"] and
            verdict.get('addressComplete') and
            not verdict.get('hasInferredComponents') and
            not verdict.get('hasReplacedComponents') and
            not verdict.get('unconfirmedComponentTypes') and
            not verdict.get('missingComponentTypes') and
            not verdict.get('unresolvedTokens')
        )
        if region_code == "US" and enable_usps_cass:
            is_confirmed = is_confirmed and (usps_data.get('dpvConfirmation') == 'Y')

      
        return {
            "original_address": original_address,
            "validated_address": formatted_address,
            "is_valid": is_valid,
            "validation_status": status,
            "is_po_box": is_po_box,
            "is_dpv_confirmed": is_dpv_confirmed,
            "is_confirmed": is_confirmed,
            "is_vacant": usps_data.get('dpvVacant', False) ,
            "is_no_stat": usps_data.get('dpvNoStat', False) ,
            "is_cmra": usps_data.get('dpvCmra', False) ,
            "is_undeliverable": usps_data.get('undeliverable', False) ,
            "dpv_confirmation_description": dpv_confirmation_description
        }
        
    except Exception as e:
        return {
            "original_address": original_address,
            "validated_address": original_address,
            "is_valid": False,
            "validation_status": f"Parse Error: {str(e)}",
            "lat": None,
            "lng": None,
            "usps_data": None
        }

def process_addresses(df, address_column, api_key, enable_cass=False):
    """
    Process addresses from dataframe
    """
    results = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    invalid_addresses_container = st.container()
    
    total_addresses = len(df)
    
    with invalid_addresses_container:
        st.subheader("🚨 Invalid Addresses (Streaming)")
        invalid_placeholder = st.empty()
        invalid_count = 0
    
    for idx, row in df.iterrows():
        address = str(row[address_column])
        
        # Update progress
        progress = (idx + 1) / total_addresses
        progress_bar.progress(progress)
        status_text.text(f"Processing address {idx + 1} of {total_addresses}: {address[:50]}...")
        
        # Validate address
        validation_result = validate_address_google(address, api_key, enable_cass)
        parsed_result = parse_validation_result(validation_result, address)
        
        # Add original row data
        result_row = row.to_dict()
        result_row.update(parsed_result)
        results.append(result_row)
        
        # Stream invalid addresses
        if not parsed_result["is_valid"]:
            invalid_count += 1
            with st.container():
                st.error(f"**Address {idx + 1}:** {address}")
                st.write(f"**Status:** {parsed_result['validation_status']}")
                st.write(f"**Suggested:** {parsed_result['validated_address']}")
                st.write("---")
        
        # Rate limiting - Google allows 100 requests per minute
        time.sleep(0.6)  # Sleep for 600ms between requests
    
    status_text.text(f"✅ Processing complete! Found {invalid_count} invalid addresses out of {total_addresses}")
    return pd.DataFrame(results)

def download_dataframe(df, filename):
    """
    Create download link for dataframe
    """
    # Convert to Excel
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Validated_Addresses')
    
    output.seek(0)
    
    # Create download button
    st.download_button(
        label=f"📥 Download {filename}",
        data=output,
        file_name=filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

def main():
    st.title("🏠 Click2mail Address Validation Tool")
    st.markdown("Upload a CSV or Excel file with addresses and validate them using Click2mail's Address Validation API")
    
    # Sidebar for API configuration
    with st.sidebar:
        st.header("⚙️ Configuration")
        
        api_key = st.text_input(
            "Google API Key",
            type="password",
            help="Enter your Google Address Validation API key"
        )
        
        enable_cass = st.checkbox(
            "Enable USPS CASS",
            help="Enable USPS Coding Accuracy Support System for US addresses"
        )
        
        if not api_key:
            st.warning("⚠️ Please enter your Google API key to proceed")
    
    # File upload
    uploaded_file = st.file_uploader(
        "Choose a CSV or Excel file",
        type=['csv', 'xlsx', 'xls'],
        help="Upload a file containing addresses to validate"
    )
    
    if uploaded_file is not None and api_key:
        try:
            # Read the file
            if uploaded_file.name.endswith('.csv'):
                df = pd.read_csv(uploaded_file)
            else:
                df = pd.read_excel(uploaded_file)
            
            st.success(f"✅ File uploaded successfully! Found {len(df)} rows")
            
            # Show file preview
            with st.expander("📋 File Preview"):
                st.dataframe(df.head())
            
            # Select address column
            address_columns = df.columns.tolist()
            selected_column = st.selectbox(
                "Select the column containing addresses:",
                address_columns,
                help="Choose which column contains the addresses to validate"
            )
            
            if st.button("🚀 Start Address Validation", type="primary"):
                if len(df) > 100:
                    st.warning("⚠️ Large dataset detected. This may take a while and consume API quota.")
                    if not st.checkbox("I understand and want to proceed"):
                        st.stop()
                
                # Process addresses
                with st.spinner("Processing addresses..."):
                    validated_df = process_addresses(df, selected_column, api_key, enable_cass)
                
                # Display results
                st.header("📊 Validation Results")
                
                # Summary statistics
                total_addresses = len(validated_df)
                valid_addresses = len(validated_df[validated_df['is_valid'] == True])
                invalid_addresses = total_addresses - valid_addresses
                
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Total Addresses", total_addresses)
                with col2:
                    st.metric("Valid Addresses", valid_addresses)
                with col3:
                    st.metric("Invalid Addresses", invalid_addresses)
                
                # Show validation results
                with st.expander("📋 Detailed Results"):
                    st.dataframe(validated_df)
                
                # Download options
                st.header("📥 Download Results")
                
                col1, col2 = st.columns(2)
                
                with col1:
                    st.subheader("All Addresses")
                    download_dataframe(validated_df, "all_validated_addresses.xlsx")
                
                with col2:
                    st.subheader("Corrected Addresses Only")
                    corrected_df = validated_df[validated_df['is_valid'] == True]
                    if len(corrected_df) > 0:
                        download_dataframe(corrected_df, "corrected_addresses_only.xlsx")
                    else:
                        st.info("No valid addresses found")
                
                # Invalid addresses summary
                invalid_df = validated_df[validated_df['is_valid'] == False]
                if len(invalid_df) > 0:
                    st.header("❌ Invalid Addresses Summary")
                    st.dataframe(invalid_df[['original_address', 'validated_address', 'validation_status']])
                    
                    with col2:
                        st.subheader("Invalid Addresses Only")
                        download_dataframe(invalid_df, "invalid_addresses.xlsx")
        
        except Exception as e:
            st.error(f"❌ Error processing file: {str(e)}")
    
    elif uploaded_file is not None and not api_key:
        st.warning("⚠️ Please enter your Google API key in the sidebar to proceed")
    
    # Instructions
    with st.expander("📖 Instructions"):
        st.markdown("""
        ### How to use this tool:
        
        1. **Get a Google API Key:**
           - Go to the [Google Cloud Console](https://console.cloud.google.com/)
           - Enable the Address Validation API
           - Create an API key with appropriate restrictions
        
        2. **Prepare your file:**
           - Upload a CSV or Excel file containing addresses
           - Ensure addresses are in a single column
        
        3. **Configure settings:**
           - Enter your API key in the sidebar
           - Optionally enable USPS CASS for US addresses
        
        4. **Run validation:**
           - Select the column containing addresses
           - Click "Start Address Validation"
           - Monitor the streaming output for invalid addresses
        
        5. **Download results:**
           - Download all addresses with validation results
           - Download only corrected/valid addresses
           - Download only invalid addresses for review
        
        ### Features:
        - ✅ Real-time streaming of invalid addresses
        - ✅ Support for CSV and Excel files
        - ✅ Google Address Validation API integration
        - ✅ Optional USPS CASS validation
        - ✅ Multiple download options
        - ✅ Geocoding data (lat/lng) included
        - ✅ Rate limiting to respect API quotas
        """)

if __name__ == "__main__":
    main()
