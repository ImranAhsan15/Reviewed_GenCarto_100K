# 🗺️ ArcGIS Pro Tool Instructions

> ⚠️ **Please read the following instructions carefully before using the tool.**

---

## ⚙️ Tool Requirements

- **Schema Compatibility**: This tool is developed for **old schema** only.
- **ArcGIS Pro Version**: Ensure you are using **ArcGIS Pro 3.4.2**.
- **Required Extensions**:
  - Production Mapping
  - Spatial Analyst

---

## 📂 Data Preparation Guidelines

1. **Topo Dataset Required**  
   To extract feature classes from `.gdb`, a **Topo dataset** is necessary.

2. **Feature Class Records**  
   All feature classes **must contain records**.

3. **Naming Accuracy**  
   Double-check **feature class name spellings**.

4. **No Manual Edits**  
   Do **not insert any rows or columns** in the current version.

5. **Color Cell Handling**  
   Refer to the **Excel instruction tab**:
   - Do **not delete** the colored cell.
   - Do **not move** it from its original position.

---

## 📁 File Paths

- **All necessary data**: `Z:\People\RShounok\GenCarto50K\`
- **Testing Geodatabase Path**: `Z:\People\RShounok\GenCarto50K\GK11_JOB_327545_backup.gdb`
- **Map Files**: `'Mapx' Directory`. I've prepared the Map files For 01_DataPrep and 08_ApplyCarto. Prepare Other themes accordingly, according to Sheet 0_MapPackageConfig.
- **Hierarchy File**: `HierarchyAll_50K.csv`
- **Config File**: `GenCarto100k_Config_File_v3.xlsx`
- **VST Workspace**: `Z:\People\RShounok\GenCarto50K\ProductLibraryCarto.gdb`
- **Style file for 50k**: `'Layrx_files' directory`
- **Layer file**: `no_outline.lyr` must be in the **same folder** as the input workspace.

---

## ✅ Notes

- Ensure all feature classes are correctly named and populated.
- Do not modify the structure of the Excel template.
- Keep `no_outline.lyr` in the correct location to avoid rendering issues.
