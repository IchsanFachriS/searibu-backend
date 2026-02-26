import netCDF4 as nc

ds = nc.Dataset('data/TPXO9_atlas_v5.nc', 'r')

print("Scale factor:", ds.variables['hRe'].scale_factor)
print("Sample hRe (raw int16):", ds.variables['hRe'][0, 100, 200])
print("Sample hRe (scaled):", ds.variables['hRe'][0, 100, 200] * ds.variables['hRe'].scale_factor)

ds.close()