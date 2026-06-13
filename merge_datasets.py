# """
# merge_datasets.py
# =================
# Merges South Asian images into main dataset folders
# """

# import os
# import shutil
# from pathlib import Path

# def merge_datasets(main_dir, south_asian_dir):
#     """
#     Move images from subfolders into main disease folders
#     """
#     diseases = ['Normal', 'AMD', 'Cataract', 'Diabetic_Retinopathy', 'Glaucoma']
    
#     main_path = Path(main_dir)
#     sa_path = Path(south_asian_dir)
    
#     total_moved = 0
    
#     for disease in diseases:
#         sa_disease_folder = sa_path / disease
#         main_disease_folder = main_path / disease
        
#         if not sa_disease_folder.exists():
#             print(f"⚠️  No {disease} folder in South Asian dataset")
#             continue
        
#         # Ensure main folder exists
#         main_disease_folder.mkdir(exist_ok=True)
        
#         # Copy all images
#         images = list(sa_disease_folder.glob('*'))
        
#         for img in images:
#             if img.suffix.lower() in ['.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp']:
#                 dest = main_disease_folder / img.name
                
#                 # If file already exists, add a number
#                 if dest.exists():
#                     stem = img.stem
#                     ext = img.suffix
#                     counter = 1
#                     while dest.exists():
#                         dest = main_disease_folder / f"{stem}_{counter}{ext}"
#                         counter += 1
                
#                 shutil.move(str(img), str(dest))
#                 total_moved += 1
#                 print(f"✅ {img.name} → {disease}/")
        
#         print(f"   📁 {disease}: {len(images)} images merged")
    
#     print(f"\n{'='*50}")
#     print(f"✅ TOTAL MERGED: {total_moved} images")
    
#     # Try to delete empty south asian folder
#     try:
#         shutil.rmtree(sa_path)
#         print(f"🗑️  Deleted: {south_asian_dir}")
#     except:
#         print(f"⚠️  Could not delete: {south_asian_dir}")


# def main():
#     import argparse
#     parser = argparse.ArgumentParser(description='Merge datasets')
#     parser.add_argument('--main_dir', type=str, required=True,
#                        help='Main raw_data folder')
#     parser.add_argument('--sa_dir', type=str, required=True,
#                        help='South Asian dataset folder inside raw_data')
    
#     args = parser.parse_args()
    
#     print("=" * 50)
#     print("🔄 MERGING DATASETS")
#     print("=" * 50)
#     print(f"Main: {args.main_dir}")
#     print(f"SA:   {args.sa_dir}")
#     print()
    
#     merge_datasets(args.main_dir, args.sa_dir)
    
#     # Show final counts
#     print("\n📊 Final Image Counts:")
#     for disease in ['Normal', 'AMD', 'Cataract', 'Diabetic_Retinopathy', 'Glaucoma']:
#         folder = Path(args.main_dir) / disease
#         if folder.exists():
#             count = len(list(folder.glob('*')))
#             print(f"   {disease:30s}: {count} images")


# if __name__ == '__main__':
#     main()