#!/usr/bin/env python3
"""
Inspect TPXO9 NetCDF file structure to identify correct variable names
"""

import sys
from netCDF4 import Dataset
from pathlib import Path

def inspect_tpxo(filepath):
    """Inspect TPXO NetCDF file and print structure"""
    print(f"Opening: {filepath}")
    print("=" * 80)
    
    ds = Dataset(filepath, 'r')
    
    # Print dimensions
    print("\nDIMENSIONS:")
    print("-" * 80)
    for dim_name, dim in ds.dimensions.items():
        print(f"  {dim_name}: {len(dim)}")
    
    # Print variables
    print("\nVARIABLES:")
    print("-" * 80)
    for var_name, var in ds.variables.items():
        dims = ', '.join(var.dimensions)
        shape = ' x '.join(str(s) for s in var.shape)
        dtype = var.dtype
        print(f"  {var_name}")
        print(f"    Dimensions: ({dims})")
        print(f"    Shape: {shape}")
        print(f"    Type: {dtype}")
        
        # Print attributes
        if var.ncattrs():
            print(f"    Attributes:")
            for attr in var.ncattrs():
                attr_val = getattr(var, attr)
                # Truncate long attributes
                if isinstance(attr_val, str) and len(attr_val) > 60:
                    attr_val = attr_val[:60] + "..."
                print(f"      {attr}: {attr_val}")
        print()
    
    # Print global attributes
    print("\nGLOBAL ATTRIBUTES:")
    print("-" * 80)
    for attr in ds.ncattrs():
        print(f"  {attr}: {getattr(ds, attr)}")
    
    ds.close()
    print("\n" + "=" * 80)
    print("Inspection complete")

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("Usage: python inspect_tpxo.py <path_to_tpxo_file>")
        sys.exit(1)
    
    filepath = Path(sys.argv[1])
    if not filepath.exists():
        print(f"Error: File not found: {filepath}")
        sys.exit(1)
    
    inspect_tpxo(filepath)